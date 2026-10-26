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
- `POST /v1/fetch` validates `url` (http/https), optional `prompt` and `max_length` (1–100000); the upstream fetches the page and summarizes it to markdown: `{"object": "fetch.result", "url", "title", "content", ...}`. URLs that target the internal network (`localhost`, private, link-local or other non-public IP addresses, `.local` / `.internal` hostnames) are rejected before any upstream request, on the REST endpoint (400), the MCP `web_fetch` tool and gateway-executed `web_fetch` server tools alike. The check is lexical: hostnames that only resolve to internal addresses (for example via DNS) are not detected.
- Declaring server-side web tools — Anthropic `web_search_*` / `web_fetch_*` types or OpenAI `web_search` / `web_search_options` — makes the gateway execute them on the agenttool endpoints and return protocol-native blocks (`server_tool_use` + `web_search_tool_result` / `web_fetch_tool_result`, or `web_search_call`). Each executed tool adds one billed model round-trip; `max_uses`, domain filters and `search_context_size` are honored, `user_location` is ignored.
- Search results are not lost between turns. The model sees each result's title, link and snippet; emitted `web_search_result` blocks carry the snippet in a gateway-owned `encrypted_content` value (`cb2api:` prefix) and `web_search_call` items list `action.sources`. Clients that echo these items back (`server_tool_use`, `web_search_tool_result`, `web_fetch_tool_result`, `web_search_call`) get them replayed to the model as plain text context, not as tool calls. Other `encrypted_content` values, such as real Anthropic blobs, are ignored. Dated OpenAI types such as `web_search_preview_2025_03_11` and `web_search_2025_08_26` are accepted like their undated forms.
- Gateway-executed tools do not create audit records of their own: each shows up as one `server_tool` attempt (`model` `web-search` or `web-fetch`, outcome, error code, profile, credential, tries, duration) inside the request that triggered it, which keeps its own model, profile and outcome. Its usage is the sum over every upstream round.

## Media generation

Image and video endpoints backed by the upstream `/v2/images/*` and `/v2/videos/*` services; each request is billed with upstream credits.

- `POST /v1/images/generations` — `{"prompt", "model?", "size?", "n?" (≤4), "quality?", "response_format?"}` → `{"created", "data": [{url | b64_json, revised_prompt?}], "usage"?}`. Prompts are passed through verbatim, newlines included (≤32000 characters); gemini-family models ignore `n` and `quality`, matching the official CLI.
- `POST /v1/images/edits` — OpenAI-style JSON: `{"prompt", "images": [{"image_url": "https://… | data:…"}], "model?", "size?", "input_fidelity?"}`; multipart is not supported. Image references (including the video `image_url`) accept up to 20 MiB of text, so `data:` URIs fit.
- `POST /v1/videos` — `{"prompt?" , "image_url?", "seconds?" (int or numeric string), "resolution?" ("768P"|"2K"), "aspect_ratio?", "enable_audio?", "model?"}` → `{"id", "object": "video", "status": "queued", …}`; poll with `GET /v1/videos/{id}` and fetch finished videos via `GET /v1/videos/{id}/content`.
- Without the asset base URL (`media_public_base_url`, WebUI **Media**) images always return `b64_json` and saved videos expose no link; videos are always stored locally under `media/` and the link appears once the base URL is set. Assets are served publicly at `/v1/media/{images|videos}/{id}` with immutable caching.
- Defaults are fixed per family: `hunyuan-image-v3.0` (text to image), `hunyuan-image-v2.0-general-edit` (edit), `minimax-video-h3` (video). Models outside the static list ∪ account catalog are rejected with 400, and media models are refused on the chat endpoints with a pointer here. Upstream stays the final judge of model authorization.
- Accounts are picked per request from verified media-capable products only (currently `cn-cli`), require catalog membership and a positive balance, and a refused request (401/403/429) may be retried once on another account per the failover setting, but upstream restriction codes are never replayed and an account-restriction code disables that account, as on the chat endpoints; 5xx and timeouts are never replayed to avoid double billing. Video tasks always stay bound to the account that submitted them.
- Multi-image requests download concurrently and return the surviving items when some fail; only a total failure answers 502 and leaves no stored files. Video downloads fail permanently only when the signature is gone (403/404/410), the type is refused or the file is over quota; repeated queries then return the same error. Any other failure (network, timeout, 408/429/5xx, local storage errors) keeps the task active, shows a `message` retry hint on the in-progress task, and the next query retries.

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
