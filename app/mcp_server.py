#!/usr/bin/env python3
"""Stateless Model Context Protocol endpoint exposing the web_search and web_fetch tools.

One streamable-HTTP surface, no sessions and no server-initiated requests: each POST
carries exactly one JSON-RPC message. The 2026-07-28 era is validated per request via
the MCP-Protocol-Version header plus params._meta; requests without that _meta are
served under the 2025-06-18/2025-11-25 rules (initialize still negotiates).
"""

from __future__ import annotations

from pathlib import Path

from app import web_fetch, web_search

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
HEADER_MISMATCH = -32020
UNSUPPORTED_PROTOCOL_VERSION = -32022
NEWEST_VERSION = "2026-07-28"
SUPPORTED_VERSIONS = (NEWEST_VERSION, "2025-11-25", "2025-06-18")
LEGACY_INIT = "initialize"
VERSION_KEY = "protocolVersion"
LIST_TTL_MS = 600000
VERSION = (
    Path(__file__)
    .resolve()
    .parent.with_name("VERSION")
    .read_text(encoding="utf-8")
    .strip()
)
SERVER_INFO = {
    "name": "codebuddy2api",
    "title": "codebuddy2api web search",
    "version": VERSION,
}

TOOL = {
    "name": "web_search",
    "title": "Web Search",
    "description": "Search the public web and return titled result links with snippets.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query text."},
            "count": {
                "type": "integer",
                "minimum": 1,
                "maximum": web_search.MAX_RESULTS,
                "description": "Maximum number of results to return.",
            },
        },
        "required": ["query"],
    },
    "annotations": {"readOnlyHint": True, "openWorldHint": True},
}


FETCH_TOOL = {
    "name": "web_fetch",
    "title": "Web Fetch",
    "description": "Fetch a URL and return its content as summarized markdown.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The http(s) URL to fetch."},
            "prompt": {"type": "string", "description": "What to extract from the page."},
        },
        "required": ["url"],
    },
    "annotations": {"readOnlyHint": True, "openWorldHint": True},
}

TOOLS = [TOOL, FETCH_TOOL]


async def dispatch(headers, message, tools):
    """Handle one decoded JSON-RPC message; returns (status, body-or-None).

    `tools` maps tool names to async callables returning (result, problem_text).
    Requests carrying params._meta.protocolVersion follow the 2026-07-28 rules;
    everything else is served under the legacy eras (initialize negotiates).
    """
    if not isinstance(message, dict):
        return 400, error_body(None, JSONRPC_PARSE_ERROR, "Parse error")
    if message.get("id") is None:
        # Notifications receive no response; the server never issues requests itself.
        return 202, None
    params = message.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    if isinstance(meta, dict) and meta.get(VERSION_KEY) is not None:
        return await _modern(headers, message, tools)
    return await _legacy(headers, message, tools)


def error_body(identifier, code, message):
    return {
        "jsonrpc": "2.0",
        "id": identifier,
        "error": {"code": code, "message": message},
    }


def _header_version(headers):
    value = headers.get("mcp-protocol-version") if headers is not None else None
    return value.strip() if isinstance(value, str) else None


async def _legacy(headers, message, tools):
    identifier = message.get("id")
    version = _header_version(headers)
    if version not in (None, *SUPPORTED_VERSIONS):
        return 400, error_body(
            identifier,
            UNSUPPORTED_PROTOCOL_VERSION,
            f"unsupported MCP-Protocol-Version header; supported: {', '.join(SUPPORTED_VERSIONS)}",
        )
    method = message.get("method")
    if method == LEGACY_INIT:
        return 200, _result(
            identifier, _initialize(message.get("params"), default="2025-11-25")
        )
    if method == "ping":
        return 200, _result(identifier, {})
    if method == "tools/list":
        return 200, _result(identifier, {"tools": TOOLS})
    if method == "tools/call":
        return await _call_tool(message, tools)
    return 200, error_body(
        identifier, JSONRPC_METHOD_NOT_FOUND, f"Method not found: {method}"
    )


async def _modern(headers, message, tools):
    identifier = message.get("id")
    method = message.get("method")
    meta_version = message["params"]["_meta"][VERSION_KEY]
    if meta_version != NEWEST_VERSION:
        return 400, error_body(
            identifier,
            UNSUPPORTED_PROTOCOL_VERSION,
            f"params._meta.{VERSION_KEY} must be {NEWEST_VERSION}; supported: {', '.join(SUPPORTED_VERSIONS)}",
        )
    header_version = _header_version(headers)
    if header_version != meta_version:
        return 400, error_body(
            identifier,
            HEADER_MISMATCH,
            "MCP-Protocol-Version header must match the request _meta version",
        )
    mcp_method = headers.get("mcp-method")
    if isinstance(mcp_method, str) and mcp_method.strip() != method:
        return 400, error_body(
            identifier,
            HEADER_MISMATCH,
            "MCP-Method header must match the request method",
        )
    if method == "initialize":
        return 200, _result(identifier, _complete(_initialize(message.get("params"))))
    if method == "ping":
        return 200, _result(identifier, _complete({}))
    if method == "tools/list":
        return 200, _result(
            identifier,
            _complete({"tools": TOOLS, "ttlMs": LIST_TTL_MS, "cacheScope": "public"}),
        )
    if method == "tools/call":
        return await _call_tool(message, tools)
    return 404, error_body(
        identifier, JSONRPC_METHOD_NOT_FOUND, f"Method not found: {method}"
    )


def _initialize(params, *, default=NEWEST_VERSION):
    requested = params.get(VERSION_KEY) if isinstance(params, dict) else None
    negotiated = requested if requested in SUPPORTED_VERSIONS else default
    return {
        "protocolVersion": negotiated,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": SERVER_INFO,
    }


def _complete(payload):
    return {"resultType": "complete", **payload}


def _result(identifier, payload):
    return {"jsonrpc": "2.0", "id": identifier, "result": payload}


async def _call_tool(message, tools):
    identifier = message.get("id")
    params = message.get("params")
    name = params.get("name") if isinstance(params, dict) else None
    if name not in tools:
        return 200, error_body(
            identifier, JSONRPC_METHOD_NOT_FOUND, f"Unknown tool: {name}"
        )
    arguments = params.get("arguments") if isinstance(params, dict) else None
    if name == FETCH_TOOL["name"]:
        url, prompt, problem = _fetch_arguments(arguments)
        if problem:
            return 200, _result(identifier, _tool_error(problem))
        result, problem = await tools[name](url, prompt)
        if problem:
            return 200, _result(identifier, _tool_error(problem))
        text = web_fetch.result_text(result)
    else:
        query, count, problem = _arguments(arguments)
        if problem:
            return 200, _result(identifier, _tool_error(problem))
        results, problem = await tools[name](query, count)
        if problem:
            return 200, _result(identifier, _tool_error(problem))
        text = web_search.search_text(query, results)
    return 200, _result(
        identifier,
        _complete(
            {
                "content": [{"type": "text", "text": text}],
                "isError": False,
            }
        ),
    )

def _arguments(arguments):
    if not isinstance(arguments, dict):
        return "", 0, "invalid arguments: expected an object"
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        return "", 0, "invalid arguments: query must be a non-empty string"
    count = arguments.get("count")
    if count is None:
        return query.strip(), web_search.DEFAULT_RESULTS, ""
    if (
        isinstance(count, bool)
        or not isinstance(count, (int, float))
        or not float(count).is_integer()
    ):
        return "", 0, "invalid arguments: count must be an integer"
    number = int(count)
    if number < 1 or number > web_search.MAX_RESULTS:
        return (
            "",
            0,
            f"invalid arguments: count must be between 1 and {web_search.MAX_RESULTS}",
        )
    return query.strip(), number, ""


def _fetch_arguments(arguments):
    if not isinstance(arguments, dict):
        return "", "", "invalid arguments: expected an object"
    url = arguments.get("url")
    if not isinstance(url, str) or not url.strip().lower().startswith(("http://", "https://")):
        return "", "", "invalid arguments: url must be an http(s) URL"
    prompt = arguments.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        return "", "", "invalid arguments: prompt must be a string"
    return url.strip(), prompt or "", ""


def _tool_error(message):
    return {"content": [{"type": "text", "text": message}], "isError": True}
