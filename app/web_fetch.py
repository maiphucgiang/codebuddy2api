#!/usr/bin/env python3
"""Upstream /agenttool/v1/webfetch request shaping and response parsing."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import unquote, urlsplit

DEFAULT_MAX_LENGTH = 20000
MAX_LENGTH = 100000
FETCH_TIMEOUT = 60
INTERNAL_HOST_SUFFIXES = (".localhost", ".local", ".internal")


def canonical_host(url) -> str:
    """Host as a WHATWG URL parser (browsers, Node) derives it; empty when unusable.

    urlsplit alone disagrees with those parsers: they read ``\\`` as ``/``, percent-decode
    the host and map fullwidth or ideographic-dot spellings through IDNA. Checking any other
    host than the one the fetcher connects to is a policy bypass, so every host check shares this.
    """
    try:
        host = unquote(urlsplit(str(url).replace("\\", "/")).hostname or "")
        host = host.encode("idna").decode("ascii").lower().rstrip(".")
    except ValueError:  # Includes UnicodeError, e.g. an empty or over-long label.
        return ""
    # Decoded control characters or spaces never form a valid host.
    return "" if any(char <= " " or char == "\x7f" for char in host) else host


def _internal_address(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            # inet_aton also takes the legacy IPv4 spellings resolvers honor (127.1, 2130706433, 0x7f000001).
            address = ipaddress.IPv4Address(socket.inet_aton(host))
        except OSError:
            return False
    return not address.is_global


def validate_url(url) -> str:
    """Return the stripped URL after rejecting non-http(s) and internal-network targets.

    Hosts are checked lexically: the upstream performs the fetch, so a gateway-side
    DNS lookup would not describe what it connects to.
    """
    text = url.strip() if isinstance(url, str) else ""
    host = canonical_host(text)
    if not host or not text.lower().startswith(("http://", "https://")):
        raise ValueError("url 必须是有效的 http(s) 地址")
    if host == "localhost" or host.endswith(INTERNAL_HOST_SUFFIXES) or _internal_address(host):
        raise ValueError("url 不能指向内网地址")
    return text


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
        raise ValueError("网页抓取响应必须是 JSON 对象")
    code = payload.get("code")
    if code not in (None, 0, "0"):
        raise ValueError(f"网页抓取失败：{payload.get('msg') or code}")
    content = payload.get("content")
    if not isinstance(content, str) or not content:
        raise ValueError("网页抓取响应缺少正文")
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
