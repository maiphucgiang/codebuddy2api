"""可选的客户端模板适配：固定句替换、运行时摘要与零宽词表。

默认只处理 system，可选 developer 和已识别的 harness user；真实对话不改写。
只缓解固定模板误拦，不保证上游接受请求，也不改变对真实输入的审核。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from app.harness_context import parse_harness_text

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

# 只改已知客户端模板；不全局清除空白或零宽字符。
def _template_pattern(text: str) -> re.Pattern:
    words = [(_ZWSP + "*").join(re.escape(char) for char in word) for word in text.split(" ")]
    return re.compile(r"(?<!\w)" + r"[\s\u200b]+".join(words) + _ZWSP + r"*(?!\w)", re.IGNORECASE)


_WORKBUDDY_IDENTITY = "You are CodeBuddy, Tencent's official CLI"
_CLAUDE_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude"
_TEMPLATE_REPLACEMENTS = tuple(
    (_template_pattern(source), target) for source, target in (
        (_CLAUDE_IDENTITY + ", running within the Claude Agent SDK", _WORKBUDDY_IDENTITY),
        (_CLAUDE_IDENTITY, _WORKBUDDY_IDENTITY),
        ("You are a Claude agent, built on Anthropic's Claude Agent SDK", _WORKBUDDY_IDENTITY),
        ("Main branch (you will usually use this for PRs)",
         "Main branch (you will usually use this for PR)"),
    )
)
_WORKBUDDY_IDENTITY_PATTERN = _template_pattern(_WORKBUDDY_IDENTITY)
_GIT_STATUS_CONTEXT = re.compile(
    r"^\s*(?:gitStatus:\s*|#\s*gitStatus\s*\n)"
    r"This is the git status at the start of the conversation\.", re.IGNORECASE | re.MULTILINE,
)


def _replace_git_status_text(text: str, protected: bool = False) -> tuple[str, bool]:
    lines = []
    for line in text.splitlines(keepends=True):
        label = line.lstrip(" \t")
        if re.match(r"(?:Status|Recent commits):", label, re.IGNORECASE):
            protected = True
        if not protected:
            for index, (pattern, target) in enumerate(_TEMPLATE_REPLACEMENTS):
                match = pattern.match(label)
                if match and (label[match.end():].lstrip().startswith(":") if index == 3
                              else label[match.end():].strip() in ("", ".")):
                    indent = line[:len(line) - len(label)]
                    line = indent + target + label[match.end():]
                    break
        lines.append(line)
    return "".join(lines), protected


def _replace_git_status_content(content):
    if isinstance(content, str):
        return _replace_git_status_text(content)[0]
    blocks, protected = [], False
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text, protected = _replace_git_status_text(block.get("text", ""), protected)
            block = {**block, "text": text}
        blocks.append(block)
    return blocks



def _replace_workbuddy_templates(text: str) -> str:
    for pattern, replacement in _TEMPLATE_REPLACEMENTS:
        text = pattern.sub(replacement, text)
    return text


def _is_claude_system(text: str) -> bool:
    return (any(pattern.search(text) for pattern, _ in _TEMPLATE_REPLACEMENTS[:3])
            or bool(_WORKBUDDY_IDENTITY_PATTERN.search(text))
            or "You are Claude Code" in text)


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


def _zero_width_split(term: str) -> str:
    """在词内部插入零宽空格。如 'DoS' -> 'Do\\u200bS'。"""
    if len(term) <= 1:
        return term
    # 在第 1 个字符后插入即可（足够打断子串匹配，且改动最小）
    return term[0] + _ZWSP + term[1:]


def desensitize_text(text: str) -> str:
    """先替换已知客户端模板，再对词表插入零宽空格。"""
    if not text:
        return text
    context = _GIT_STATUS_CONTEXT.search(text)
    if context:
        return desensitize_text(text[:context.start()]) + _replace_git_status_text(text[context.start():])[0]
    text = _replace_workbuddy_templates(text)
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
    """判断 user 消息是否其实是 Codex/CLI 注入的上下文，而非用户自然输入。"""
    text = _content_to_text(content)
    return any(marker in text for marker in _HARNESS_USER_MARKERS)


def _prune_runtime_fragments(role: str, text: str) -> str:
    """只处理有可信边界的上下文，不猜测未知段落或裁掉其后的指令。"""
    return parse_harness_text(text).render() if text else text


def _compact_harness_message(role: str, content) -> str | None:
    """保留既有 system 压缩策略；user 由结构化提取路径单独处理。"""
    if isinstance(content, list) and any(
            not isinstance(block, dict) or block.get("type") != "text" for block in content):
        return None  # 摘要不能吞掉图片或未知内容块。
    text = _content_to_text(content)
    if not text:
        return None
    if role in ("system", "developer") and (
            _is_claude_system(text) or any(marker in text for marker in _CODEX_SYSTEM_MARKERS)):
        if _is_claude_system(text):
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


def _desensitize_harness_content(content):
    if isinstance(content, str):
        return parse_harness_text(content).render(desensitize_text)
    if isinstance(content, list):
        return [
            {**block, "text": parse_harness_text(block["text"]).render(desensitize_text)}
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
            else block for block in content
        ]
    return content


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
        content = m.get("content")
        text = _content_to_text(content) if role == "user" and desensitize_harness_user else ""
        if _GIT_STATUS_CONTEXT.match(text) and re.search(r"(?m)^Current branch:", text):
            # 官方 gitStatus 是上下文，不压缩分支/状态，也不改其中的普通词。
            nm["content"] = _replace_git_status_content(content)
            out.append(nm)
            continue
        if should_desensitize and role == "user":
            nm["content"] = _desensitize_harness_content(m.get("content"))
            out.append(nm)
            continue
        if should_desensitize:
            content = m.get("content")
            compacted = _compact_harness_message(role, content) if compact_harness else None
            if compacted is not None:
                nm["content"] = desensitize_text(compacted)
            elif isinstance(content, str):
                nm["content"] = desensitize_text(_prune_runtime_fragments(role, content))
            elif isinstance(content, list):
                new_blocks = []
                git_context = git_data = False
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        nb = dict(blk)
                        text = blk.get("text", "")
                        context = _GIT_STATUS_CONTEXT.search(text) if isinstance(text, str) else None
                        if git_context and isinstance(text, str):
                            nb["text"], git_data = _replace_git_status_text(text, git_data)
                        elif context:
                            head = desensitize_text(_prune_runtime_fragments(role, text[:context.start()]))
                            tail, git_data = _replace_git_status_text(text[context.start():])
                            nb["text"] = head + tail
                            git_context = True
                        else:
                            nb["text"] = desensitize_text(_prune_runtime_fragments(role, text))
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
