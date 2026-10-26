#!/usr/bin/env python3
"""Upstream /agenttool/v1/search request shaping and response parsing for REST and MCP consumers."""
from __future__ import annotations

DEFAULT_RESULTS = 5
MAX_RESULTS = 10
QUERY_TIMEOUT = 20


def request_body(query: str, max_results: int = DEFAULT_RESULTS, *,
                 allowed_domains=None, freshness: str = "") -> dict:
    count = max(1, min(int(max_results or DEFAULT_RESULTS), MAX_RESULTS))
    body = {"query": query, "type": "text2text", "max_results": count}
    if isinstance(allowed_domains, (list, tuple)):
        domains = [str(domain).strip() for domain in allowed_domains
                   if str(domain).strip()]
        if domains:
            body["allowed_domains"] = domains
    if isinstance(freshness, str) and freshness.strip():
        body["freshness"] = freshness.strip()
    return body


def parse_results(payload) -> list:
    """Validate the upstream envelope and return its result items unchanged; raise ValueError on unusable bodies."""
    if not isinstance(payload, dict):
        raise ValueError("搜索响应必须是 JSON 对象")
    code = payload.get("code")
    if code not in (None, 0, "0"):
        raise ValueError(f"搜索失败：{payload.get('msg') or code}")
    results = payload.get("results")
    if not isinstance(results, list):
        raise ValueError("搜索响应缺少 results 列表")
    return [item for item in results
            if isinstance(item, dict) and isinstance(item.get("url"), str) and item["url"]]


def search_text(query: str, results: list) -> str:
    """Render results as numbered markdown links with footnote-style snippets for MCP clients."""
    lines = [f"{index}. [{item.get('title') or item['url']}]({item['url']})"
             for index, item in enumerate(results, 1)]
    snippets = [(index, str(item.get("snippet") or "").strip()) for index, item in enumerate(results, 1)]
    snippets = [(index, snippet) for index, snippet in snippets if snippet]
    if snippets:
        lines.append("")
        lines.extend(f"[{index}] {snippet}" for index, snippet in snippets)
    return "\n".join(lines)
