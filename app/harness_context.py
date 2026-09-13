"""按可信闭合边界提取 CLI 上下文；不确定的文本保留为用户内容。"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Callable


_TAG_SUMMARIES = {
    "environment_context": "Environment context is provided by the harness.",
    "permissions instructions": (
        "Runtime permissions apply: filesystem access may be sandboxed, network may be restricted, "
        "and some commands may require user approval."
    ),
    "collaboration_mode": "Collaboration mode instructions are provided by the harness.",
    "skills_instructions": "Runtime skill metadata is available. Use relevant skills only when explicitly requested or clearly applicable.",
    "plugins_instructions": "Runtime plugin metadata is available when relevant.",
    "system-reminder": None,
    "INSTRUCTIONS": None,
}
_HEADINGS = {
    "# AGENTS.md instructions": "Repository instructions and durable user context are provided.",
    "# claudeMd": "Repository CLAUDE.md instructions are provided.",
}
_TAG_PATTERN = re.compile(r"<(/?)(" + "|".join(map(re.escape, _TAG_SUMMARIES)) + r")>")
_HEADING_PATTERN = re.compile(r"[ \t]{0,3}(" + "|".join(map(re.escape, _HEADINGS)) + r")(?=[ \t\r\n]|$)")
_FENCE_PATTERN = re.compile(r"[ \t]{0,3}(`{3,}|~{3,})")
_MAX_DEPTH = 32
_MAX_BLOCKS = 8192


@dataclass(frozen=True)
class HarnessPart:
    text: str
    context: bool


@dataclass(frozen=True)
class HarnessText:
    parts: tuple[HarnessPart, ...]

    @property
    def matched(self) -> bool:
        return any(part.context for part in self.parts)

    @property
    def user_text(self) -> str:
        return "".join(part.text for part in self.parts if not part.context)

    @property
    def context_text(self) -> str:
        return "".join(part.text for part in self.parts if part.context)

    def render(self, context_transform: Callable[[str], str] | None = None) -> str:
        return "".join(context_transform(part.text) if part.context and context_transform else part.text
                       for part in self.parts)


@dataclass
class _Block:
    name: str
    start: int
    inner_start: int
    inner_end: int = 0
    end: int = 0
    children: list[_Block] = field(default_factory=list)


def _literal(text: str) -> HarnessText:
    return HarnessText((HarnessPart(text, False),))


def _render_block(text: str, block: _Block) -> str:
    summary = _HEADINGS.get(block.name, _TAG_SUMMARIES.get(block.name))
    if summary is not None:
        return "\n\n" + summary + "\n\n"
    cursor, parts = block.inner_start, []
    for child in block.children:
        parts.extend((text[cursor:child.start], _render_block(text, child)))
        cursor = child.end
    parts.append(text[cursor:block.inner_end])
    return "\n\n" + "".join(parts) + "\n\n"


def parse_harness_text(text: str) -> HarnessText:
    """只识别行首/相邻结构标签，保留代码围栏、内联引用和未闭合块。"""
    if not text or not any(marker in text for marker in ("<", "#")):
        return _literal(text)
    roots: list[_Block] = []
    stack: list[_Block] = []
    offset = count = 0
    pending_heading = False
    fence = ""

    def append(block: _Block) -> None:
        (stack[-1].children if stack else roots).append(block)

    for line in text.splitlines(keepends=True):
        line_fence = _FENCE_PATTERN.match(line)
        if fence:
            if (line_fence and line_fence[1][0] == fence[0] and len(line_fence[1]) >= len(fence)
                    and not line[line_fence.end():].strip()):
                fence = ""
            offset += len(line)
            continue
        if line_fence:
            fence = line_fence[1]
            pending_heading = False
            offset += len(line)
            continue

        heading = _HEADING_PATTERN.match(line)
        if heading:
            count += 1
            if count > _MAX_BLOCKS:
                return _literal(text)
            end = offset + heading.end(1)
            append(_Block(heading[1], offset + heading.start(1), end, end, end))
            pending_heading = not line[heading.end(1):].strip()
            offset += len(line)
            continue

        previous = 0
        at_boundary = True
        for token in _TAG_PATTERN.finditer(line):
            between = line[previous:token.start()]
            at_boundary = at_boundary and not between.strip()
            closing, name = token.groups()
            if closing:
                if stack and stack[-1].name == name:
                    block = stack.pop()
                    block.inner_end = offset + token.start()
                    block.end = offset + token.end()
                    append(block)
                    at_boundary = True
                else:
                    at_boundary = False
                pending_heading = False
            elif at_boundary and (name != "INSTRUCTIONS" or pending_heading or stack):
                count += 1
                if len(stack) >= _MAX_DEPTH or count > _MAX_BLOCKS:
                    return _literal(text)
                stack.append(_Block(name, offset + token.start(), offset + token.end()))
                at_boundary = True
                pending_heading = False
            else:
                at_boundary = False
                pending_heading = False
            previous = token.end()
        if line[previous:].strip():
            pending_heading = False
        offset += len(line)

    # 未闭合父块没有进入 roots，其内部即使有闭合子块也不会被单独裁剪。
    if not roots:
        return _literal(text)
    parts: list[HarnessPart] = []
    cursor = 0
    for block in roots:
        if block.start > cursor:
            parts.append(HarnessPart(text[cursor:block.start], False))
        parts.append(HarnessPart(_render_block(text, block), True))
        cursor = block.end
    if cursor < len(text):
        parts.append(HarnessPart(text[cursor:], False))
    return HarnessText(tuple(parts))
