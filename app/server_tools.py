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
import secrets

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


def _declared_domains(value) -> list | None:
    """Normalize declared domain filters: None means undeclared, never an empty list."""
    domains = _domain_list(value)
    return domains or None


def _search_config(tool_def: dict) -> dict:
    size = str(tool_def.get("search_context_size") or "medium").lower()
    filters = tool_def.get("filters") if isinstance(tool_def.get("filters"), dict) else {}
    allowed = _declared_domains(tool_def.get("allowed_domains") or filters.get("allowed_domains"))
    blocked = _declared_domains(tool_def.get("blocked_domains") or filters.get("blocked_domains"))
    if allowed and blocked:
        blocked = None
    return {
        "max_uses": tool_def.get("max_uses"),
        "allowed_domains": allowed,
        "blocked_domains": blocked,
        "max_results": CONTEXT_SIZE_RESULTS.get(size, 5),
    }


def _fetch_config(tool_def: dict) -> dict:
    tokens = tool_def.get("max_content_tokens")
    max_length = min(100000, int(tokens) * 4) if isinstance(tokens, int) and tokens > 0 else None
    allowed = _declared_domains(tool_def.get("allowed_domains"))
    blocked = _declared_domains(tool_def.get("blocked_domains"))
    if allowed and blocked:
        blocked = None
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
    # Client-owned tool names win: drop spec entries that would collide.
    taken = _tool_names(plain)
    spec = {name: config for name, config in spec.items() if name not in taken}
    return plain, spec


def _tool_names(tools) -> set:
    """Collect declared tool names from both Anthropic and Chat tool shapes."""
    names = set()
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not isinstance(name, str):
            function = tool.get("function")
            name = function.get("name") if isinstance(function, dict) else None
        if isinstance(name, str) and name:
            names.add(name)
    return names

def injected_chat_tools(spec: dict, taken_names=()) -> list:
    """Build Chat function tools for the spec, skipping client-owned names."""
    taken = {name for name in taken_names if isinstance(name, str)}
    result = []
    if TOOL_SEARCH in spec and TOOL_SEARCH not in taken:
        result.append(SEARCH_FUNCTION)
    if TOOL_FETCH in spec and TOOL_FETCH not in taken:
        result.append(FETCH_FUNCTION)
    return result


def merge_injected(tools, spec: dict) -> tuple[list, dict]:
    """Append injectable spec tools to a tool list; returns (tools, active_spec).

    ``active_spec`` holds only the entries actually injected, so execution and
    injection always agree on which names the gateway owns.
    """
    injected = injected_chat_tools(spec, _tool_names(tools if isinstance(tools, list) else []))
    active = {tool["function"]["name"]: spec[tool["function"]["name"]] for tool in injected}
    return (list(tools) if isinstance(tools, list) else []) + injected, active

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
              "input": {}, "error": None, "error_code": None}
    try:
        if name == TOOL_SEARCH:
            query = str(arguments.get("query") or "").strip()
            if not query:
                raise ValueError("query is required")
            query += _blocked_suffix(_domain_list(arguments.get("blocked_domains")))
            # The declaration bounds every search; model-given domains may only
            # narrow it, and undeclared stays undeclared (never widened).
            declared_allowed = config["allowed_domains"]
            declared_blocked = config["blocked_domains"]
            per_call_allowed = _domain_list(arguments.get("allowed_domains"))
            upstream_allowed = declared_allowed or per_call_allowed
            results = await search_fn(query, config["max_results"],
                                      upstream_allowed, str(arguments.get("freshness") or ""))
            if declared_allowed or declared_blocked:
                results = [item for item in results
                           if _domain_allowed(str(item.get("url") or ""),
                                              declared_allowed or [],
                                              declared_blocked or [])]
            record["input"] = {"query": query}
            record["results"] = results
        else:
            url = str(arguments.get("url") or "").strip()
            if not url.lower().startswith(("http://", "https://")):
                raise ValueError("url must be an http(s) URL")
            if not _domain_allowed(url, config["allowed_domains"] or [],
                                   config["blocked_domains"] or []):
                raise ValueError("URL is blocked by the web fetch domain policy")
            content = await fetch_fn(url, str(arguments.get("prompt") or ""),
                                     config.get("max_length"))
            record["input"] = {"url": url}
            record["content"] = content
    except ValueError as error:
        record["error"] = str(error) or "invalid tool input"
        record["error_code"] = "invalid_tool_input"
    except Exception as error:  # Upstream/tool failures surface as error records.
        record["error"] = str(error) or error.__class__.__name__
        record["error_code"] = "unavailable"
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
    ``body["messages"]`` with assistant/tool turns. Continuation rounds relax
    ``tool_choice`` to ``auto``; the original choice and tool list are restored
    on exit. Returns ``(result, records)``; when the upstream asks for client
    tools the loop stops and the returned result carries only those calls.
    """
    records: list[dict] = []
    executed = {name: 0 for name in INJECTED_NAMES}
    saved_choice = body.get("tool_choice")
    saved_tools = body.get("tools")
    try:
        result = await fetch_chat(body)
        for _ in range(max_rounds):
            choice = (result.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            calls = message.get("tool_calls") or []
            if choice.get("finish_reason") != "tool_calls" or not calls:
                return result, records
            gateway_calls = [call for call in calls
                             if (call.get("function") or {}).get("name") in spec]
            client_calls = [call for call in calls if call not in gateway_calls]
            body["messages"].append(_assistant_message(message))
            for call in gateway_calls:
                function = call.get("function") or {}
                name = function.get("name")
                record = await _execute_record(name, function, spec, executed,
                                               search_fn, fetch_fn)
                record["id"] = call.get("id") or record["id"]
                record["public_id"] = _public_id()
                body["messages"].append(_tool_message(record))
                records.append(record)
            if client_calls:
                # Client tools belong to the caller: surface their calls and stop;
                # executed gateway calls stay recorded as protocol blocks.
                return _strip_gateway_calls(result, client_calls), records
            # Continuations answer with text, so a required/named choice cannot
            # hold for them; relax it for every follow-up round.
            body["tool_choice"] = "auto"
            result = await fetch_chat(body)
        # Round budget exhausted: force a plain answer without the injected tools.
        if isinstance(body.get("tools"), list):
            body["tools"] = [tool for tool in body["tools"]
                             if (tool.get("function") or {}).get("name") not in spec]
        result = await fetch_chat(body)
        return result, records
    finally:
        body["tool_choice"] = saved_choice
        if "tool_choice" in body and saved_choice is None:
            del body["tool_choice"]
        body["tools"] = saved_tools
        if "tools" in body and saved_tools is None:
            del body["tools"]


async def _execute_record(name, function, spec, executed, search_fn, fetch_fn) -> dict:
    """Run one gateway tool call, or emit the max_uses error record."""
    if _uses_left(spec, name, executed[name]):
        record = await execute_call(name, _arguments(function), spec[name],
                                    search_fn=search_fn, fetch_fn=fetch_fn)
        executed[name] += 1
        return record
    return {"kind": "fetch" if name == TOOL_FETCH else "search",
            "name": "web_fetch" if name == TOOL_FETCH else "web_search",
            "id": "", "public_id": _public_id(),
            "input": _arguments(function), "error": "max_uses_exceeded",
            "error_code": "max_uses_exceeded"}


def _public_id() -> str:
    return "srvtoolu_" + secrets.token_hex(8)


def _strip_gateway_calls(result: dict, client_calls: list) -> dict:
    """Return the upstream round keeping only the client-side tool calls."""
    choices = list(result.get("choices") or [])
    if not choices:
        return result
    message = dict(choices[0].get("message") or {})
    message["tool_calls"] = client_calls
    first = dict(choices[0])
    first["message"] = message
    return {**result, "choices": [first, *choices[1:]]}


def _arguments(function: dict) -> dict:
    raw = function.get("arguments") or "{}"
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
