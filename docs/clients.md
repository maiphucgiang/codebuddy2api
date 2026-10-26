# Client configuration

[Home](../README.md) · [简体中文](clients.zh-CN.md)

Add an account in the [WebUI](webui.md) first. Replace `YOUR_GATEWAY_API_KEY` with the gateway API key and `MODEL_ID` with a public model ID. Adjust addresses and ports for your deployment.

## Common settings

| Protocol | Base URL |
|----------|----------|
| OpenAI Chat / Responses | `http://127.0.0.1:8787/v1` |
| Anthropic Messages | `http://127.0.0.1:8787` |

Domestic/international and CLI/WorkBuddy accounts use the same URLs. Backend routing is automatic; no `/cn` or `/intl` prefix is needed. Clients use the WebUI login key, not its management Cookie.

List available models:

```bash
curl http://127.0.0.1:8787/v1/models \
  -H 'Authorization: Bearer YOUR_GATEWAY_API_KEY'
```

Use an ID from this response or the WebUI; accounts do not necessarily support the same models. Public aliases can be configured in the UI. Anthropic model names are not automatically guessed or mapped.

## Codex CLI

Merge this into `~/.codex/config.toml`; do not overwrite existing configuration. A ready-to-copy variant is kept at [`examples/codex-codebuddy.example.toml`](../examples/codex-codebuddy.example.toml):

```toml
[model_providers.workbuddy]
name = "WorkBuddy (via local converter)"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
env_key = "CODEBUDDY2API_KEY"

[profiles.workbuddy]
model = "MODEL_ID"
model_provider = "workbuddy"
```

```bash
export CODEBUDDY2API_KEY='YOUR_GATEWAY_API_KEY'
codex --profile workbuddy "your task"
```

Codex uses `/v1/responses`. Runtime context is processed separately from real instructions; oversized requests return HTTP 413 rather than silently truncating the latest user request.

## Responses projection

Responses projection is a server setting; client addresses do not change. Balanced mode is the default, while `passthrough` disables Responses projection completely. To change generated assistant/tool truncation limits, see [Responses projection](advanced.md#responses-projection).

## Claude Code / CC Switch

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
export ANTHROPIC_AUTH_TOKEN='YOUR_GATEWAY_API_KEY'
export ANTHROPIC_MODEL='MODEL_ID'
claude
```

- Claude Code, Anthropic SDKs and CC Switch's Anthropic provider use a Base URL without `/v1/messages`; SDKs append the path themselves. Only clients explicitly asking for a complete endpoint should include `/v1/messages`.
- `POST /v1/messages` is converted to upstream Chat Completions, retaining native tool calls and reasoning content.
- `--desensitize` controls WorkBuddy's fixed CLI-template adaptation. It is off by default and enabled in the published image. See the [advanced reference](advanced.md) for options such as retaining fuller instructions.

## Other OpenAI-compatible clients

Use the common Base URL, API key and model ID with Cherry Studio, ZCode, LobeChat, NextChat, Open WebUI or your own SDK client.

The generation endpoints are `POST /v1/chat/completions`, `POST /v1/responses` and `POST /v1/messages`. Set `stream: false` explicitly for JSON responses or `stream: true` for SSE.

Streaming policy is a server setting, not a client request field. The default `compatible` mode aggregates Responses and tool-bearing Chat/Messages as before; `realtime` sends all three protocols incrementally. Non-streaming requests remain validated JSON in either mode. See [Streaming modes](advanced.md#streaming-modes) for configuration, partial-output errors and the loss of automatic tool-argument regeneration.

## Web search and fetch

Search and fetch endpoints backed by the upstream agenttool `/v1/search` and `/v1/webfetch`; no model is called and no tokens are billed.

| Client | Setup |
|--------|-------|
| MCP clients (Claude Code, Codex CLI, …) | Streamable-HTTP server at `http://127.0.0.1:8787/v1/mcp` exposing `web_search` and `web_fetch` tools |
| REST clients | `POST /v1/search` with `{"query": "...", "max_results": 5}`, `POST /v1/fetch` with `{"url": "...", "prompt": "...", "max_length": 20000}` |

```bash
claude mcp add --transport http web-search http://127.0.0.1:8787/v1/mcp \
  --header "Authorization: Bearer YOUR_API_KEY"
codex mcp add web-search --url http://127.0.0.1:8787/v1/mcp --bearer-token-env-var CODEBUDDY2API_KEY

curl http://127.0.0.1:8787/v1/search -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{"query": "rust edition guide", "max_results": 5}'

curl http://127.0.0.1:8787/v1/fetch -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{"url": "https://example.com/guide", "prompt": "summarize"}'
```

- Requests use the same API key as the other `/v1` endpoints.
- `POST /v1/search` validates `query` and `max_results` (1–10) and returns the upstream result items unchanged: `{"object": "search.results", "query", "total_results", "results": [{title, url, snippet, ...}]}`.
- The gateway picks an account per request and fails over once per gateway failover settings; deterministic upstream rejections are returned as-is.
- `POST /v1/fetch` validates `url` (http/https), optional `prompt` and `max_length` (1–100000); the upstream fetches the page and summarizes it to markdown: `{"object": "fetch.result", "url", "title", "content", ...}`.
- Declaring server-side web tools — Anthropic `web_search_*` / `web_fetch_*` types or OpenAI `web_search` / `web_search_options` — makes the gateway execute them on the agenttool endpoints and return protocol-native blocks (`server_tool_use` + `web_search_tool_result` / `web_fetch_tool_result`, or `web_search_call`). Each executed tool adds one billed model round-trip; `max_uses`, domain filters and `search_context_size` are honored, `user_location` is ignored.

## Protocol behavior worth knowing

- `developer` messages become `system`; the first system message is placed first before matching tool results, without mutating the original payload.
- Chat accepts mixed Anthropic `tool_use` / `tool_result` history, preserving call IDs, arguments, result images and error markers; ordinary `thinking` becomes `reasoning_content`, not visible text. Native Chat fields stay unchanged.
- Conflicting fields, unmatched tool results, unsupported mixed blocks and `redacted_thinking` return HTTP 400 before routing. Split user messages accept only `role` and `content`, with all `tool_result` blocks before ordinary text/images; Anthropic thinking signatures are not forwarded.
- Messages `thinking` blocks and Responses readable `reasoning` items are retained as assistant `reasoning_content`, including full-history tool continuations; encrypted-only history returns HTTP 400.
- Messages `thinking` / `output_config.effort` and Responses `reasoning.effort` map to upstream reasoning controls. See [reasoning compatibility](advanced.md#reasoning-compatibility) for defaults and budget limits.
- Named function choices are sent upstream as `required` with only that function available. Responses matches the exact `(namespace, name)`; unknown or historical-only choices return HTTP 400.
- Responses keeps current and historical tool identities distinct. Schema cleanup removes only boolean `encrypted` markers in schema positions, preserving property names, definitions and literal data.
- Namespaced tools use Chat-safe aliases of at most 64 characters, including nested and long identities; Responses output and history retain the original namespace and name. Plain tool names stay unchanged.
- Errors follow the client protocol's own shape (OpenAI `error` object vs Anthropic `{"type":"error"}`), and status codes are preserved. Realtime mode can deliver useful deltas before a later invalid terminal, disconnect or size error; valid truncation/filter distinctions remain native. Clients must not treat an opened SSE connection as proof of successful completion.
- `POST /v1/messages/count_tokens` returns a character-based heuristic estimate for budgeting, not an exact count.

See the [advanced reference](advanced.md#request-boundaries) for the full request-processing rules.
