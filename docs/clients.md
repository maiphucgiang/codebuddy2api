# Client configuration

[Home](../README.md) · [简体中文](clients.zh-CN.md)

Add an account in the [WebUI](webui.md) and create a client key in **Keys**. Replace `YOUR_GATEWAY_API_KEY` with that inference key and `MODEL_ID` with a public model ID. Adjust addresses and ports for your deployment.

## Common settings

| Protocol | Base URL |
|----------|----------|
| OpenAI Chat / Responses | `http://127.0.0.1:8787/v1` |
| Anthropic Messages | `http://127.0.0.1:8787` |

Domestic/international and CLI/WorkBuddy accounts use the same URLs. Backend routing is automatic; no `/cn` or `/intl` prefix is needed. Clients use an inference key from **Keys** or the external admin key, never the WebUI management Cookie.
List available models:

```bash
curl http://127.0.0.1:8787/v1/models \
  -H 'Authorization: Bearer YOUR_GATEWAY_API_KEY'
```

Use an ID from this response or the WebUI; accounts do not necessarily support the same models. Public aliases can be configured in the UI. Anthropic model names are not automatically guessed or mapped.

## API keys and authorization

Standard keys can be named, revealed, copied, rotated, enabled or disabled, and restricted by expiry, IP/CIDR allowlist, per-minute requests and source model IDs. Admin keys are unrestricted and shown only at creation or rotation; only their hashes are stored. An externally configured admin key is read-only in the UI. Every key is valid only for `/v1/*`, never `/admin/*`; standard keys cannot read `/v1/dashboard/billing/*`, while admin keys and explicitly anonymous deployments can.

| Status / code | Meaning |
|---------------|---------|
| 401 `api_key_required` / `invalid_api_key` | No key was supplied, the key is unknown, or a database admin key is overridden by the external key |
| 401 `api_key_disabled` / `api_key_expired` | The key is disabled or expired |
| 403 `ip_not_allowed` / `model_not_allowed` | The client IP or resolved source model is outside the key's allowlist |
| 403 `admin_key_required` | A standard key attempted to read account-pool billing |
| 429 `rpm_exceeded` | The key exceeded its 60-second sliding-window limit; honor `Retry-After` |

OpenAI responses use `error.type` and `error.code`; Anthropic uses `authentication_error`, `permission_error` or `rate_limit_error`. With no keys at all, `OPEN_NOAUTH=false` returns `api_key_required`; explicitly enabled anonymous mode accepts missing or placeholder tokens and also exposes billing.

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


## Embeddings

`POST /v1/embeddings` exposes `baai/bge-m3` and `Qwen/Qwen3-Embedding-0.6B` through `/codebase/openai/v1/embeddings`. It accepts text, text arrays and token-ID arrays, and returns 1024-dimensional `float` or `base64` vectors. `baai/bge-m3` maps to upstream `model2`; only a missing codebase route or connection failure falls back to `/v2/embeddings` as `bge-m3`. Qwen has no paid fallback.

Codebase usage has been observed as zero, but that is not a billing guarantee. Routing uses enabled domestic accounts with positive balances, shared alias/binding rules and 16 in-flight requests per account; timeouts and 5xx responses do not switch endpoints.

```bash
curl http://127.0.0.1:8787/v1/embeddings \
  -H "Authorization: Bearer YOUR_GATEWAY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"baai/bge-m3","input":["first document","second document"],"encoding_format":"float"}'
```

## Reranking

`POST /v1/rerank` exposes the fixed codebase reranker as `reranker`. Send `query`, 1–100 string `documents`, optional `top_n` and `return_documents`; results contain ordered indices and optional source documents, never relevance scores. The service truncates each document at roughly 600 characters. Its weights are not exposed: `bge-reranker-v2-m3` and `Qwen3-Reranker` are unverified candidates, not confirmed identities.

```bash
curl http://127.0.0.1:8787/v1/rerank \
  -H "Authorization: Bearer YOUR_GATEWAY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"reranker","query":"capital of France","documents":["Berlin","Paris"],"top_n":1}'
```
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
- Images are stored locally by default. Without the asset base URL (`media_public_base_url`, WebUI **Media**) they return `b64_json` without a public link; an explicit `response_format=b64_json` skips image persistence. Videos are always stored locally. Setting the base URL enables `url` responses and video links; assets are served at `/v1/media/{images|videos}/{id}` with immutable caching.
- Defaults are fixed per family: `hunyuan-image-v3.0` (text to image), `hunyuan-image-v2.0-general-edit` (edit), `minimax-video-h3` (video). Media models share `/v1/models` and WebUI **Model routing**, including aliases, enable switches and binding rules; chat endpoints reject them with a pointer here.
- Standard keys can poll and fetch only video tasks they created; other keys' task IDs return the same 404 as missing tasks. Public `/v1/media/...` URLs rely on unguessable IDs and remain valid until media TTL expiry, even after the creating key is disabled or deleted.
- Accounts are picked per request from verified media-capable products only (currently `cn-cli`) and require a positive balance. Static media models defer authorization to upstream even when absent from an account catalog; dynamic models require exact media catalog tags. A refused request (401/403/429) may be retried once on another allowed account per the failover setting, but upstream restriction codes are never replayed and account restrictions disable the account (matching chat behavior); 5xx and timeouts are never replayed to avoid double billing. Video task polling stays bound to the submitting account.
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
