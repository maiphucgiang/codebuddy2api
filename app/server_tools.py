#!/usr/bin/env python3
"""Gateway-executed web tools declared through protocol server-tool syntax.

Clients may declare Anthropic ``web_search_*`` / ``web_fetch_*`` server tools or
OpenAI ``web_search`` tools. The gateway swaps them for the WorkBuddy-CLI-shaped
function tools the upstream models already know, executes the calls itself
against the agenttool endpoints, and returns protocol-native blocks.
"""
from __future__ import annotations

import json
import re

TOOL_SEARCH = "WebSearch"
TOOL_FETCH = "WebFetch"
INJECTED_NAMES = (TOOL_SEARCH, TOOL_FETCH)

# Anthropic server tool types (any dated version) and OpenAI hosted search types.
SEARCH_TYPE_RE = re.compile(r"^web_search(_\d{8})?$")
FETCH_TYPE_RE = re.compile(r"^web_fetch_\d{8}$")
OPENAI_SEARCH_TYPES = frozenset({"web_search", "web_search_preview"})

# search_context_size (OpenAI) → result count (upstream max is 10).
CONTEXT_SIZE_RESULTS = {"low": 3, "medium": 5, "high": 8}
MAX_ROUNDS = 4

SEARCH_FUNCTION = {
    "type": "function",
    "function": {
        "name": TOOL_SEARCH,
        "description": "Search the web for current information.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "The search query to use."},
                "allowed_domains": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Only include search results from these domains."},
                "blocked_domains": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Never include search results from these domains"},
                "freshness": {
                    "type": "string",
                    "description": ("Filter results by time range: d[N], m[N] or y[N] "
                                    "for the last N days, months or years.")},
            },
            "required": ["query"],
        },
    },
}

FETCH_FUNCTION = {
    "type": "function",
    "function": {
        "name": TOOL_FETCH,
        "description": "Fetch content from a URL and answer a prompt about it.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to fetch content from"},
                "prompt": {"type": "string",
                           "description": "The prompt to run on the fetched content"},
            },
            "required": ["url"],
        },
    },
}


def _domain_list(value) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip().lstrip(".") for item in value if str(item).strip()]


def _search_config(tool_def: dict) -> dict:
    size = str(tool_def.get("search_context_size") or "medium").lower()
    filters = tool_def.get("filters") if isinstance(tool_def.get("filters"), dict) else {}
    allowed = _domain_list(tool_def.get("allowed_domains") or filters.get("allowed_domains"))
    blocked = _domain_list(tool_def.get("blocked_domains") or filters.get("blocked_domains"))
    if allowed and blocked:
        blocked = []
    return {
        "max_uses": tool_def.get("max_uses"),
        "allowed_domains": allowed,
        "blocked_domains": blocked,
        "max_results": CONTEXT_SIZE_RESULTS.get(size, 5),
    }


def _fetch_config(tool_def: dict) -> dict:
    tokens = tool_def.get("max_content_tokens")
    max_length = min(100000, int(tokens) * 4) if isinstance(tokens, int) and tokens > 0 else None
    allowed = _domain_list(tool_def.get("allowed_domains"))
    blocked = _domain_list(tool_def.get("blocked_domains"))
    if allowed and blocked:
        blocked = []
    return {
        "max_uses": tool_def.get("max_uses"),
        "allowed_domains": allowed,
        "blocked_domains": blocked,
        "max_length": max_length,
    }


def search_config_from_options(options) -> dict:
    """Build a search spec entry from an OpenAI web_search_options object."""
    return _search_config(options if isinstance(options, dict) else {})


def split_server_tools(tools, *, openai_types: bool = False) -> tuple[list, dict]:
    """Separate plain client tools from declared server web tools.

    Returns ``(plain_tools, spec)`` where spec maps injected tool names to their
    normalized execution config; an empty spec means nothing to adapt.
    """
    plain: list = []
    spec: dict = {}
    if not isinstance(tools, list):
        return plain, spec


    for tool in tools:
        if not isinstance(tool, dict):
            plain.append(tool)
            continue
        kind = str(tool.get("type") or "")
        if SEARCH_TYPE_RE.match(kind):
            spec.setdefault(TOOL_SEARCH, _search_config(tool))
            continue
        if FETCH_TYPE_RE.match(kind):
            spec.setdefault(TOOL_FETCH, _fetch_config(tool))
            continue
        if openai_types and kind in OPENAI_SEARCH_TYPES:
            spec.setdefault(TOOL_SEARCH, _search_config(tool))
            continue
        plain.append(tool)
    return plain, spec


def injected_chat_tools(spec: dict, taken_names=()) -> list:
    """Build Chat function tools for the spec, skipping client-owned names."""
    taken = {name for name in taken_names if isinstance(name, str)}
    result = []
    if TOOL_SEARCH in spec and TOOL_SEARCH not in taken:
        result.append(SEARCH_FUNCTION)
    if TOOL_FETCH in spec and TOOL_FETCH not in taken:
        result.append(FETCH_FUNCTION)
    return result


def _blocked_suffix(blocked: list) -> str:
    return "".join(f" -site:{domain}" for domain in blocked)


def _hostname(url: str) -> str:
    match = re.match(r"^[a-z][a-z0-9+.-]*://([^/:?#]+)", url, re.IGNORECASE)
    return (match.group(1).lower() if match else url.lower()).lstrip(".")


def _domain_allowed(url: str, allowed: list, blocked: list) -> bool:
    host = _hostname(url)
    for domain in blocked:
        if host == domain or host.endswith("." + domain):
            return False
    if allowed:
        return any(host == domain or host.endswith("." + domain) for domain in allowed)
    return True


def _uses_left(spec: dict, name: str, executed: int) -> bool:
    limit = spec.get(name, {}).get("max_uses")
    return not isinstance(limit, int) or limit <= 0 or executed < limit


def _search_tool_text(record: dict) -> str:
    if record.get("error"):
        return f"web search failed: {record['error']}"
    lines = [f"- [{item.get('title') or item.get('url')}]({item.get('url')})"
             for item in record.get("results") or []]
    return "\n".join(lines) or "No search results found."


def _fetch_tool_text(record: dict) -> str:
    if record.get("error"):
        return f"web fetch failed: {record['error']}"
    return str((record.get("content") or {}).get("content") or "")


async def execute_call(name: str, arguments: dict, config: dict, *,
                       search_fn, fetch_fn) -> dict:
    """Execute one injected tool call; never raises, failures become error records."""
    record = {"kind": "fetch" if name == TOOL_FETCH else "search",
              "name": "web_fetch" if name == TOOL_FETCH else "web_search",
              "id": f"srv_{name.lower()}_{id(arguments) & 0xffffff:x}",
              "input": {}, "error": None}
    try:
        if name == TOOL_SEARCH:
            query = str(arguments.get("query") or "").strip()
            if not query:
                raise ValueError("query is required")
            query += _blocked_suffix(_domain_list(arguments.get("blocked_domains")))
            allowed = _domain_list(arguments.get("allowed_domains")) or config["allowed_domains"]
            results = await search_fn(query, config["max_results"],
                                      allowed, str(arguments.get("freshness") or ""))
            if config["blocked_domains"] and not allowed:
                # Tool-level blocked domains are enforced on the results too.
                results = [item for item in results
                           if _domain_allowed(str(item.get("url") or ""),
                                              [], config["blocked_domains"])]
            record["input"] = {"query": query}
            record["results"] = results
        else:
            url = str(arguments.get("url") or "").strip()
            if not _domain_allowed(url, config["allowed_domains"], config["blocked_domains"]):
                raise ValueError("URL is blocked by the web fetch domain policy")
            content = await fetch_fn(url, str(arguments.get("prompt") or ""),
                                     config.get("max_length"))
            record["input"] = {"url": url}
            record["content"] = content
    except Exception as error:  # Upstream/tool failures surface as error records.
        record["error"] = str(error) or error.__class__.__name__
    return record


def _tool_message(record: dict) -> dict:
    text = _fetch_tool_text(record) if record["kind"] == "fetch" else _search_tool_text(record)
    return {"role": "tool", "tool_call_id": record["id"],
            "content": text or "(empty result)"}


def _assistant_message(message: dict) -> dict:
    kept = {"role": "assistant", "content": message.get("content")}
    calls = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        calls.append({"id": call.get("id"), "type": "function",
                      "function": {"name": function.get("name"),
                                   "arguments": function.get("arguments") or "{}"}})
    kept["tool_calls"] = calls
    return kept


async def run_chat_tool_loop(fetch_chat, body: dict, spec: dict, *,
                             search_fn, fetch_fn, max_rounds: int = MAX_ROUNDS,
                             log=None):
    """Drive upstream rounds, executing injected web tools between them.

    ``fetch_chat(body)`` must return one merged Chat completion. Mutates
    ``body["messages"]`` with assistant/tool turns. Returns ``(result, records)``.
    """
    records: list[dict] = []
    executed = {name: 0 for name in INJECTED_NAMES}
    result = await fetch_chat(body)
    for _ in range(max_rounds):
        choice = (result.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        calls = message.get("tool_calls") or []
        if choice.get("finish_reason") != "tool_calls" or not calls:
            return result, records
        body["messages"].append(_assistant_message(message))
        again = False
        for call in calls:
            function = call.get("function") or {}
            name = function.get("name")
            if name in spec:
                if not _uses_left(spec, name, executed[name]):
                    record = {"kind": "fetch" if name == TOOL_FETCH else "search",
                              "name": "web_fetch" if name == TOOL_FETCH else "web_search",
                              "id": call.get("id") or "", "input": {},
                              "error": "max_uses_exceeded"}
                    record["input"] = _arguments(function)
                    body["messages"].append(_tool_message(record))
                    records.append(record)
                    continue
                record = await execute_call(name, _arguments(function), spec[name],
                                             search_fn=search_fn, fetch_fn=fetch_fn)
                record["id"] = call.get("id") or record["id"]
                body["messages"].append(_tool_message(record))
                records.append(record)
                executed[name] += 1
                again = True
            else:
                # Client tools cannot run inside the gateway loop; answer so the
                # upstream conversation stays valid and steer back to text.
                body["messages"].append(
                    {"role": "tool", "tool_call_id": call.get("id"),
                     "content": "[gateway] client tool calls are not executable here; "
                                "answer with the information you already have"})
        if not again:
            result = await fetch_chat(body)
            return result, records
        result = await fetch_chat(body)
    # Round budget exhausted: force a plain answer without the injected tools.
    tools = body.get("tools")
    if isinstance(tools, list):
        body["tools"] = [tool for tool in tools
                         if (tool.get("function") or {}).get("name") not in spec]
    result = await fetch_chat(body)
    body["tools"] = tools
    return result, records


def _arguments(function: dict) -> dict:
    raw = function.get("arguments") or "{}"
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
