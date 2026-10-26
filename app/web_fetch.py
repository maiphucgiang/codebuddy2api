#!/usr/bin/env python3
"""Upstream /agenttool/v1/webfetch request shaping and response parsing."""
from __future__ import annotations

DEFAULT_MAX_LENGTH = 20000
MAX_LENGTH = 100000
FETCH_TIMEOUT = 60


def request_body(url: str, prompt: str = "", max_length: int = DEFAULT_MAX_LENGTH) -> dict:
    """Mirror the WorkBuddy CLI webfetch body; the upstream summarizes by default."""
    length = max(1, min(int(max_length or DEFAULT_MAX_LENGTH), MAX_LENGTH))
    return {
        "url": url,
        "format": "markdown",
        "prompt": prompt or "Extract the main content of this page",
        "timeout": 20,
        "max_length": length,
    }


def parse_content(payload) -> dict:
    """Validate the response envelope and return the content fields unchanged.

    Success bodies carry no ``code`` field; error bodies are ``{code, msg}``.
    """
    if not isinstance(payload, dict):
        raise ValueError("webfetch response must be a JSON object")
    code = payload.get("code")
    if code not in (None, 0, "0"):
        raise ValueError(f"webfetch failed: {payload.get('msg') or code}")
    content = payload.get("content")
    if not isinstance(content, str) or not content:
        raise ValueError("webfetch response has no content")
    return payload


def result_text(result: dict) -> str:
    """Render fetched content as markdown for MCP-style text consumers."""
    title = str(result.get("title") or "").strip()
    url = str(result.get("url") or "").strip()
    lines = []
    if title:
        lines.append(f"# {title}" if url else title)
    if url:
        lines.append(f"Source: {url}")
    if lines:
        lines.append("")
    lines.append(str(result.get("content") or ""))
    if result.get("truncated"):
        lines.append("")
        lines.append("[truncated]")
    return "\n".join(lines)
