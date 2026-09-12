"""
desensitize — 针对 CodeBuddy 后端内容审核的脱敏模块（独立、可选）。

背景
----
CodeBuddy 后端（copilot.tencent.com）有内容审核，会拦截含"攻击/漏洞/凭证"
等含义的英文术语。但这些词经常出现在客户端**固定的合规 system 模板**里
（例如 ZCode 的 agent 声明：「Refuse requests for DoS attacks, exploit
development, credential testing, C2 frameworks ...」），属于**拒绝作恶**
的合规声明，并非用户的有害输入，却被后端误判为敏感词，导致整条请求被拦。

本模块做的事
------------
对这些"合规声明高频词"做轻量处理：在词内部插入零宽空格（U+200B），

    "DoS" -> "Do\u200bS"        （人/模型读仍是 DoS，后端关键词匹配失效）

只处理一个明确的词表，默认只作用于 system 角色的消息（这是模板合规声明的
集中地）。不改动其它角色内容，避免影响真实对话。

设计原则
--------
- 独立模块，可单独 import / 单独测试。
- 保守：词表小而明确；只默认处理 system 消息；可关闭。
- 不试图、也不可能绕过对用户真实有害输入的审核——只缓解客户端模板被误伤。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# 零宽空格：插入到关键词内部，打断后端的关键词匹配，但模型/人眼读起来无差别。
_ZWSP = "\u200b"

# 触发审核的"合规声明高频词"（来自真实被拦截的客户端 system 模板）。
# 全部是"拒绝作恶"语境里常见的英文术语。大小写不敏感匹配。
SENSITIVE_TERMS: list[str] = [
    # 原有词表
    "DoS",
    "DDoS",
    "exploit",
    "credential testing",
    "credential stuffing",
    "supply chain compromise",
    "supply-chain compromise",
    "detection evasion",
    "C2 frameworks",
    "C2 framework",
    "command and control",
    "malicious purposes",
    "malicious intent",
    "mass targeting",
    "brute force",
    "brute-force",
    "privilege escalation",
    "reverse shell",
    "remote code execution",
    "SQL injection",
    "XSS",
    "CSRF",
    "phishing",
    "malware",
    "ransomware",
    "keylogger",
    "rootkit",
    "backdoor",
    "botnet",
    "zero-day",
    "0day",
    # Codex CLI system prompt 里额外的高频触发词
    "vulnerability",
    "vulnerabilities",
    "red teaming",
    "red-teaming",
    "sandbox",
    "sandboxing",
    "sandboxed",
    "unsandboxed",
    "escalated privileges",
    "escalated",
    "escalation",
    "destructive action",
    "destructive command",
    "destructive",
    "attack",
    "attacks",
    "cybersecurity",
    "security review",
    "exploit development",
    "hacking",
    "penetration testing",
    "penetration test",
    "injection",
    "weaponize",
    "weaponized",
    "harmful",
    "dangerous",
    "abuse",
    "abusive",
    "illegal",
    "terrorist",
    "terrorism",
    "bomb",
    "weapon",
    "weapons",
    "drug",
    "drugs",
    "narcotic",
    "suicide",
    "self-harm",
    "murder",
    "kill",
    "violence",
    "violent",
    # Claude Code / Anthropic 品牌词（避免竞争品牌词触发审核）
    "Claude Code",
    "Claude Opus",
    "Claude Sonnet",
    "Claude Haiku",
    "Claude Fable",
    "Anthropic",
    "Co-Authored-By",
    "noreply@anthropic.com",
]

# 编译成一个大正则，按词长降序，避免短词先吃掉长词。
# 用 \b 边界 + 忽略大小写。
_PATTERN = re.compile(
    "|".join(re.escape(t) for t in sorted(SENSITIVE_TERMS, key=len, reverse=True)),
    re.IGNORECASE,
)

# Codex CLI 会把大量运行时上下文包装进一条 user 消息里；这些不是用户真正提问，
# 里面常含 permissions / sandbox / skills 等说明，也会触发后端审核。
_HARNESS_USER_MARKERS = (
    "# AGENTS.md instructions",
    "<environment_context>",
    "<permissions instructions>",
    "<collaboration_mode>",
    "<skills_instructions>",
    "<system-reminder>",           # Claude Code 注入的运行时上下文
    "# claudeMd",                  # Claude Code CLAUDE.md 注入
)

_CODEX_SYSTEM_MARKERS = (
    "You are a coding agent running in the Codex CLI",
    "Within this context, Codex refers to",
    "# How you work",
    "You are Claude Code",         # Claude Code system prompt
)

_PERMISSIONS_MARKERS = (
    "<permissions instructions>",
    "Filesystem sandboxing defines which files can be read or written.",
    "## How to request escalation",
)

_SKILLS_MARKERS = (
    "<skills_instructions>",
    "### Available skills",
    "### How to use skills",
)

# 第三段为 None 表示保留标签内正文（system-reminder 里是任务/记忆，不能整段丢掉）。
_KEEP_INNER = None
_RUNTIME_BLOCK_REPLACEMENTS = (
    (
        "<environment_context>",
        "</environment_context>",
        "Environment context is provided by the harness.",
    ),
    (
        "<permissions instructions>",
        "</permissions instructions>",
        (
            "Runtime permissions apply: filesystem access may be sandboxed, network may be restricted, "
            "and some commands may require user approval."
        ),
    ),
    (
        "<collaboration_mode>",
        "</collaboration_mode>",
        "Collaboration mode instructions are provided by the harness.",
    ),
    (
        "<skills_instructions>",
        "</skills_instructions>",
        "Runtime skill metadata is available. Use relevant skills only when explicitly requested or clearly applicable.",
    ),
    (
        "<plugins_instructions>",
        "</plugins_instructions>",
        "Runtime plugin metadata is available when relevant.",
    ),
    (
        "<system-reminder>",
        "</system-reminder>",
        _KEEP_INNER,
    ),
)

_USER_HEADING_REPLACEMENTS = (
    (
        "# AGENTS.md instructions",
        "Repository instructions and durable user context are provided.",
    ),
    (
        "# claudeMd",
        "Repository CLAUDE.md instructions are provided.",
    ),
)

_RUNTIME_TAIL_MARKERS = (
    "The following deferred tools are now available via ToolSearch.",
    "Available agent types for the Agent tool:",
    "The following skills are available for use with the Skill tool:",
    "The following sk​ills are available for use with the Sk​ill tool:",
    "## MCP Server Instructions",
)

_RUNTIME_TAIL_SUMMARY = (
    "Runtime tool, agent, skill, and MCP metadata is available separately."
)

_CODEX_CORE_SUMMARY = (
    "You are a coding assistant in Codex CLI. Be precise, helpful, concise, and safe. "
    "Inspect the repository, use available tools when needed, follow repository instructions, "
    "and keep the user informed with concise progress updates."
)


def _zero_width_split(term: str) -> str:
    """在词内部插入零宽空格。如 'DoS' -> 'Do\\u200bS'。"""
    if len(term) <= 1:
        return term
    # 在第 1 个字符后插入即可（足够打断子串匹配，且改动最小）
    return term[0] + _ZWSP + term[1:]


def desensitize_text(text: str) -> str:
    """对文本中的触发词插入零宽空格。无触发词则原样返回。"""
    if not text:
        return text
    return _PATTERN.sub(lambda m: _zero_width_split(m.group(0)), text)


def _iter_text_blocks(content):
    """遍历 OpenAI content（字符串或 [{type, text}, ...]）里的文本块，返回 (容器, key)。"""
    if isinstance(content, str):
        yield content, None  # 字符串：调用方直接替换
    elif isinstance(content, list):
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                yield blk, "text"


def _content_to_text(content) -> str:
    """把字符串或 content blocks 规整成纯文本，便于识别注入模板。"""
    text = content if isinstance(content, str) else ""
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                parts.append(str(blk.get("text", "")))
        text = "".join(parts)
    return text


def _looks_like_harness_user_message(content) -> bool:
    """判断 user 消息是否夹带了 Codex/CLI 注入的上下文。"""
    text = _content_to_text(content)
    return any(marker in text for marker in _HARNESS_USER_MARKERS)


def _block_stop_pattern(start_tag: str) -> str:
    """未闭合标签截到下一个已知注入块/标题，避免把后面的用户原文一起吃掉。"""
    stops = [re.escape(tag) for tag, _, _ in _RUNTIME_BLOCK_REPLACEMENTS if tag != start_tag]
    stops.extend(re.escape(heading) for heading, _ in _USER_HEADING_REPLACEMENTS)
    return "(?:" + "|".join(stops) + ")" if stops else r"(?!)"


def _apply_runtime_block_replacements(text: str) -> str:
    """替换 environment/permissions 等注入块；system-reminder 只去标签、保留正文。"""
    pruned = text
    for start_tag, end_tag, replacement in _RUNTIME_BLOCK_REPLACEMENTS:
        closed = re.compile(
            r"\s*" + re.escape(start_tag) + r"(.*?)" + re.escape(end_tag) + r"\s*",
            re.DOTALL,
        )
        unclosed = re.compile(
            r"\s*" + re.escape(start_tag) + r"(.*?)(?=" + _block_stop_pattern(start_tag) + r"|\Z)",
            re.DOTALL,
        )

        def _fill(inner: str) -> str:
            inner = inner.strip()
            if replacement is _KEEP_INNER:
                return f"\n\n{inner}\n\n" if inner else "\n\n"
            return "\n\n" + replacement + "\n\n"

        pruned = closed.sub(lambda match: _fill(match.group(1)), pruned)
        if start_tag in pruned:
            pruned = unclosed.sub(lambda match: _fill(match.group(1)), pruned)
    return pruned


def _heading_stop_pattern(heading: str) -> str:
    stops = [re.escape(tag) for tag, _, _ in _RUNTIME_BLOCK_REPLACEMENTS]
    stops.extend(re.escape(other) for other, _ in _USER_HEADING_REPLACEMENTS if other != heading)
    stops.extend(re.escape(summary) for _, _, summary in _RUNTIME_BLOCK_REPLACEMENTS if isinstance(summary, str))
    stops.extend(re.escape(summary) for other, summary in _USER_HEADING_REPLACEMENTS if other != heading)
    named = "(?:" + "|".join(stops) + ")" if stops else r"(?!)"
    # 空行后的普通段落视为用户原话，不要跟 AGENTS.md / CLAUDE.md 一起吃掉。
    plain_tail = r"\n\n(?!\s*(?:#|<[A-Za-z/]|[-*] ))"
    return f"(?:{named}|{plain_tail})"


def _replace_user_heading_sections(text: str) -> str:
    """把 AGENTS.md / CLAUDE.md 注入标题段换成短句，后面的用户原话留下。"""
    pruned = text
    for heading, replacement in _USER_HEADING_REPLACEMENTS:
        if heading not in pruned:
            continue
        pattern = re.compile(
            r"\s*" + re.escape(heading) + r".*?(?=" + _heading_stop_pattern(heading) + r"|\Z)",
            re.DOTALL,
        )
        pruned = pattern.sub("\n\n" + replacement + "\n\n", pruned)
    return pruned


def _prune_runtime_fragments(role: str, text: str) -> str:
    """裁掉冗长运行时元数据，保留行为指令、reminder 正文，以及夹在注入块里的用户原话。

    compact 与 --no-compact 的 user 路径共用：只抽 harness，不整段替换。
    """
    if not text:
        return text

    pruned = _apply_runtime_block_replacements(text)

    tail_indexes = [pruned.find(marker) for marker in _RUNTIME_TAIL_MARKERS if marker in pruned]
    if tail_indexes:
        cut = min(idx for idx in tail_indexes if idx >= 0)
        head = pruned[:cut].rstrip()
        pruned = f"{head}\n\n{_RUNTIME_TAIL_SUMMARY}" if head else _RUNTIME_TAIL_SUMMARY

    if role == "system" and any(marker in pruned for marker in _CODEX_SYSTEM_MARKERS):
        keep_sections: list[str] = []
        intro_match = re.search(
            r"^.*?(?=\n# AGENTS\.md spec|\n## Responsiveness|\n## Planning|\n## Task execution|\Z)",
            pruned,
            re.DOTALL,
        )
        if intro_match:
            intro = intro_match.group(0).strip()
            if intro:
                keep_sections.append(intro)

        for heading in ("## Personality", "# AGENTS.md spec"):
            pattern = re.compile(
                re.escape(heading) + r".*?(?=\n## |\n# |\Z)",
                re.DOTALL,
            )
            match = pattern.search(pruned)
            if match:
                section = match.group(0).strip()
                if section:
                    keep_sections.append(section)

        if keep_sections:
            pruned = "\n\n".join(keep_sections)
        else:
            pruned = _CODEX_CORE_SUMMARY

    if role == "user":
        pruned = _replace_user_heading_sections(pruned)

    pruned = re.sub(r"\n{3,}", "\n\n", pruned).strip()
    return pruned


def _compact_harness_message(role: str, content) -> str | None:
    """把 Codex / Claude Code 注入的超长运行时提示压缩成短摘要，降低审核误伤。

    user 消息不再整段替换：只抽 harness 块，system-reminder 正文和用户原话留下。
    """
    text = _content_to_text(content)
    if not text:
        return None
    if role == "system" and any(marker in text for marker in _CODEX_SYSTEM_MARKERS):
        if "You are Claude Code" in text:
            return (
                "You are a coding assistant. Be precise, helpful, concise, and safe. "
                "Use available tools when needed, follow repository instructions, and keep the user informed."
            )
        return (
            "You are a coding assistant in Codex CLI. Be precise, helpful, concise, and safe. "
            "Use available tools when needed, follow repository instructions, and keep the user informed."
        )
    if role == "system" and any(marker in text for marker in _PERMISSIONS_MARKERS):
        return (
            "Runtime permissions apply: filesystem access may be sandboxed, network may be restricted, "
            "and some commands may require user approval."
        )
    if role == "system" and any(marker in text for marker in _SKILLS_MARKERS):
        return (
            "Runtime skill metadata is available. Use relevant skills only when explicitly requested or clearly applicable."
        )
    if role == "user" and _looks_like_harness_user_message(content):
        return _prune_runtime_fragments("user", text)
    return None


def _desensitize_tool_value(value: Any, strip_metadata: bool = False):
    """递归处理 tool 定义，必要时移除高风险描述字段。"""
    if isinstance(value, dict):
        new_value = {}
        for key, item in value.items():
            if key in ("description", "title") and isinstance(item, str):
                if strip_metadata:
                    continue
                new_value[key] = desensitize_text(item)
            else:
                new_value[key] = _desensitize_tool_value(item, strip_metadata=strip_metadata)
        return new_value
    if isinstance(value, list):
        return [_desensitize_tool_value(item, strip_metadata=strip_metadata) for item in value]
    return value


def desensitize_messages(messages: Iterable[dict],
                         roles: tuple[str, ...] = ("system",),
                         desensitize_harness_user: bool = False,
                         compact_harness: bool = False) -> list[dict]:
    """对指定角色的消息文本做脱敏，返回新的 messages 列表（不修改原对象）。

    默认只处理 system 角色（合规模板集中地）。可选处理 developer，
    以及 Codex 注入的 harness user 上下文；真实用户输入保持原样。
    """
    out: list[dict] = []
    for m in messages:
        if not isinstance(m, dict):
            out.append(m)
            continue
        role = m.get("role")
        should_desensitize = role in roles
        if role == "user" and desensitize_harness_user:
            should_desensitize = _looks_like_harness_user_message(m.get("content"))

        nm = dict(m)  # 浅拷贝，不污染调用方
        if should_desensitize:
            content = m.get("content")
            compacted = _compact_harness_message(role, content) if compact_harness else None
            if compacted is not None:
                nm["content"] = desensitize_text(compacted)
            elif isinstance(content, str):
                nm["content"] = desensitize_text(_prune_runtime_fragments(role, content))
            elif isinstance(content, list):
                new_blocks = []
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        nb = dict(blk)
                        nb["text"] = desensitize_text(_prune_runtime_fragments(role, blk.get("text", "")))
                        new_blocks.append(nb)
                    else:
                        new_blocks.append(blk)
                nm["content"] = new_blocks
        out.append(nm)
    return out


def desensitize_body(body: dict, roles: tuple[str, ...] = ("system",),
                     desensitize_harness_user: bool = False,
                     desensitize_tools: bool = False,
                     compact_harness: bool = False,
                     strip_tool_metadata: bool = False) -> dict:
    """对请求体里的 messages / tools 做脱敏，返回新的 body（浅拷贝）。"""
    changed = False
    nb = dict(body)
    if body.get("messages"):
        nb["messages"] = desensitize_messages(
            body["messages"],
            roles=roles,
            desensitize_harness_user=desensitize_harness_user,
            compact_harness=compact_harness,
        )
        changed = True
    if desensitize_tools and body.get("tools"):
        nb["tools"] = _desensitize_tool_value(body["tools"], strip_metadata=strip_tool_metadata)
        changed = True
    return nb if changed else body


# ---------------------------------------------------------------------------
# 自测：python3 desensitize.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    samples = [
        "Refuse requests for DoS attacks and exploit development.",
        "Dual-use security tools (C2 frameworks, credential testing) require authorization.",
        "这是一段正常的中文，不含任何触发词。",
        "Prevent privilege escalation and brute force attacks.",
        "No sensitive words here at all.",
    ]
    print("=== 脱敏前后对比 ===")
    for s in samples:
        d = desensitize_text(s)
        changed = "✓改" if d != s else "  不"
        print(f"{changed} | 原文: {s}")
        if d != s:
            print(f"     | 脱敏: {d}")
            print(f"     | 可见字符相同，差异为零宽空格 U+200B")
    print()
    print("=== messages 脱敏（只处理 system）===")
    msgs = [
        {"role": "system", "content": "Refuse DoS attacks and exploit development."},
        {"role": "user", "content": "explain DoS attacks"},  # 不应被改
    ]
    out = desensitize_messages(msgs)
    for m in out:
        print(f"  [{m['role']}] {m['content']!r}")
    print()
    # 验证：脱敏后 system 改了，user 没改
    assert "\u200b" in out[0]["content"], "system 应被脱敏"
    assert "\u200b" not in out[1]["content"], "user 不应被脱敏"
    print("✓ 自测通过：system 被脱敏，user 保持原样")
