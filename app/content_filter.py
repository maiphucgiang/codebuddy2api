"""识别明确的上游审核拒绝；不把正文中的审核关键词当作失败。"""
from __future__ import annotations

import json
import re


FILTER_CODES = frozenset(("content_filter", "content-filter"))
_FILTER_REPLY = "抱歉，系统检测到您当前输入的信息存在敏感内容，我无法响应您的请求，请检查后重新输入"
_TEXT_LIMIT = 1024


def is_filter_text(text: str) -> bool:
    return re.sub(r"\s+", "", text).replace(",", "，").rstrip("。.!！") == _FILTER_REPLY


def is_filter_error(raw: bytes) -> bool:
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return False
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return False
    return (any(isinstance(error.get(key), str) and error[key] in FILTER_CODES for key in ("code", "type"))
            or isinstance(error.get("message"), str) and is_filter_text(error["message"]))


class ContentFilterDetector:
    """有界跟踪已校验的 SSE delta，纯拒绝才允许请求级兜底。"""

    def __init__(self):
        self.text = {"content": "", "refusal": ""}
        self.overflow = False
        self.has_tools = self.has_reasoning = False
        self.explicit = False

    def feed(self, delta: dict, finish_reason: str | None):
        self.explicit |= finish_reason in FILTER_CODES
        self.has_tools |= bool(delta.get("tool_calls"))
        self.has_reasoning |= bool(delta.get("reasoning_content"))
        for key in self.text:
            part = delta.get(key) or ""
            remaining = _TEXT_LIMIT - len(self.text[key])
            self.overflow |= len(part) > remaining
            self.text[key] += part[:remaining]

    @property
    def detected(self) -> bool:
        return self.explicit or (not self.overflow and any(is_filter_text(text) for text in self.text.values()))

    @property
    def retry_safe(self) -> bool:
        return (self.detected and not (self.overflow or self.has_tools or self.has_reasoning)
                and all(not text.strip() or is_filter_text(text) for text in self.text.values()))
