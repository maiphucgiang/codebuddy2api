# Advanced reference

[Home](../README.md) · [简体中文](advanced.zh-CN.md)

Use the [WebUI](webui.md) for everyday management. See [deployment](deployment.md) for startup methods and [client configuration](clients.md) for examples.

## Configuration and CLI

Precedence: **explicit CLI flags > environment > persisted WebUI settings > defaults**. Hot settings apply immediately; restart-marked settings require a manual restart. Change locked options in the startup configuration; the WebUI does not edit `.env`.

Compose explicitly passes some environment variables and CLI flags, so deleting a line from `.env` may not unlock it. Recreate the container after changing these values; to let the WebUI manage them, also remove the corresponding explicit Compose settings.

| Flag | Default | Description |
|------|---------|-------------|
| `--host` / `--port` | `127.0.0.1` / `8787` | Local listener |
| `--api-key` | none | Shared management and inference key; management is locked without it |
| `--admin-csrf [true/false]` | `true` | Startup-only management Origin/CSRF checks; disabling does not bypass API-key or session authentication |
| `--auth-file` | scan `auth/` | Explicit credential file, repeatable; disables scanning other files |
| `--log` | none | Additional text logs, 50 MiB rotation and 2 backups; SQLite auditing remains enabled |
| `--desensitize` | off | Adapt fixed CLI templates, compact runtime prompts and mask keywords with zero-width characters |
| `--no-compact` | off | With desensitization, retain fuller instructions while adapting templates and pruning runtime context; does not disable Responses projection |
| `--keep-tool-metadata [true/false]` | `false` | Retain tool descriptions and parameter-schema `description/title`, independently of prompt compaction |
| `--skip-check` | off | Skip startup preflight |
| `--credit-price-cny` | `0.014` | Domestic CNY per credit for billing conversion |
| `--credit-price-usd` | `0.03` | International USD per credit for billing conversion |
| `--usd-rate` | `7.15` | CNY per USD for billing conversion |
| `--model-catalog-ttl` | `21600` | Model catalog cache TTL, seconds |
| `--no-model-guard` | off | Disable the out-of-catalog guard; passthrough is limited to one product profile and still respects disabling, bindings and catalog readiness |
| `--max-images` | `16` | Total images per request; `0` permits no images |
| `--image-policy` | `truncate` | Keep newest images; `error` rejects excess images with 413 |
| `--tool-call-max-retry` | `3` | Extra generations after malformed tool calls (each consumes credits); `0` disables retries |
| `--max-inbound-bytes` | `67108864` | Raw body limit for generation and token-count POSTs, before parsing (chunked included); other routes are not buffered; 413 beyond it |
| `--max-collect-bytes` | `8388608` | Total collection budget for aggregated output (content + reasoning + tool arguments); `response_too_large` beyond it; `0` disables |
| `--max-concurrent` | `64` | Concurrency limit for the three generation endpoints only; excess requests get 503 with Retry-After; token counting is unaffected; `0` disables |
| `--max-inflight-per-account` | `0` | Per-process, per-account in-flight client inference limit; `0` disables, full accounts return 503 |
| `--upstream-keepalive [true/false]` | `false` | Bounded connection reuse isolated by official origin; requires restart |
| `--request-context-mode` | `legacy` | `scoped` enables explicit sessions and per-attempt tracing; changes apply to new requests |
| `--failover-max` | `0` | Extra credentials tried when a request fails before the first response byte reaches the client; `0` keeps the upstream behaviour of surfacing the failure directly |
| `--retry-write-timeout` | `false` | Opt a request-body write timeout into replay (fresh connection and `--failover-max`), accepting that bytes already sent may have been processed |
| `--max-request-bytes` | `33554432` | Positive byte limit for the processed upstream JSON |
| `--log-body-limit` | `65536` | Text-log body preview bytes; `0` logs summaries only, not the SQLite diagnostic budget |

Environment variables include `CODEBUDDY_AUTH_DIR`, `CODEBUDDY_IMPORT_DIR`, `CODEBUDDY2API_KEY`, `CODEBUDDY2API_ADMIN_CSRF`, `CODEBUDDY2API_KEEP_TOOL_METADATA`, `CODEBUDDY2API_LOG`, `CODEBUDDY2API_MAX_IMAGES`, `CODEBUDDY2API_IMAGE_POLICY`, `CODEBUDDY2API_MAX_REQUEST_BYTES`, `CODEBUDDY2API_LOG_BODY_LIMIT`, `CODEBUDDY2API_FAILOVER_MAX` and `CODEBUDDY2API_RETRY_WRITE_TIMEOUT`. See [deployment](deployment.md) for startup examples.

`CODEBUDDY2API_AUTO_ACCEPT_BUDDY` is startup-only and defaults to `false`. It preauthorizes enabled domestic accounts for first-Buddy onboarding, agreement and travel; automatic travel still respects its account switch. `first_buddy` needs no acceptance API: pending states, including `not_accepted`, allow one real domestic WorkBuddy conversation on that account. Prefer an eligible zero-rate model, otherwise the lowest known rate; request at most 32 output tokens with possible credit usage. Other reward tasks, paid boxes, pet switching and international trials are excluded.

Manual `POST /admin/credentials/{id}/travel` returns `buddy_confirmation` with official terms and a separate `authorization` scope. Submit `{"confirm_buddy":true,"agreement_revision":"<returned revision>"}` after consent; old adoption-only revisions are rejected. `can_claim` describes eligibility and never disables consent.

`control.sqlite3` preserves consent and one actual onboarding conversation per account across restarts. A live preflight cancellation releases only its own unsent reservation; unknown or sent attempts are never released automatically. Historical acceptance records do not block an unsent conversation. Only official task completion permits adoption. Unconfirmed first-claim sends remain reserved beyond 24 hours and only reconcile through reads; pre-claim failures may resume after backoff. Keep this database when upgrading; travel-status and balance sync remain read-only.

Travel claims and departures share an account-scoped write reservation. Uncertain results do not expire or replay; fresh status reads reconcile them without issuing upstream writes. Store failures stop claims and departures, and local readback updates preserve receipt ownership across processes.

Trial credits are manual-only for eligible `intl-work` accounts: use the credential row's claim drawer or `POST /admin/credentials/{id}/trial`. Startup, periodic maintenance and balance sync never claim. Results expose safe error categories, HTTP/business codes and retry time; response bodies are capped at 64 KiB and never returned to the browser. Success/already-claimed records persist in `auth/trial-ledger.json`; failures wait at least 24 hours before another manual attempt. Keep this file when upgrading.

`CODEBUDDY2API_AUTO_TRIAL` and `--auto-trial` are retired: old startup options warn and do nothing; saved Boolean `auto_trial` settings are ignored on load. Remove them from deployment configuration. Before reverting to older code, check these old settings to avoid re-enabling automatic claims.

### Tool metadata retention

Off by default, preserving the existing policy: desensitization strips tool descriptions, and Responses tool projection also strips them; `--no-compact` does not change this. When enabled, Chat, Responses and Messages retain supported tool descriptions and string `description/title` annotations in parameter schemas. With desensitization enabled, retained text is still processed. Prompt compaction and existing content-filter retry conditions/counts are unchanged; fallback processing also respects this option.

- **WebUI:** Settings → Keep tool descriptions; unlocked changes apply immediately and persist.
- **CLI:** append `--keep-tool-metadata` or `--keep-tool-metadata true` to the existing command; explicit `false` overrides the environment.
- **Environment:** set `CODEBUDDY2API_KEEP_TOOL_METADATA=true`. Compose passes it only when set, leaving the WebUI unlocked otherwise. Remove or comment out the variable to remove the environment lock; do not set an empty string.

Use a source/image build and Compose configuration containing this feature; recreate containers after changing their environment. Retained descriptions may increase input tokens and content-filter rejections; compatibility across accounts/models is not guaranteed. Set `false` to restore the previous policy. This option does not restore other schema fields or deep nodes removed by existing Responses projection, nor relax the request-size budget.

### Connection reuse and account capacity

Both settings are available in the WebUI; their environment variables are `CODEBUDDY2API_UPSTREAM_KEEPALIVE` and `CODEBUDDY2API_MAX_INFLIGHT_PER_ACCOUNT`. Unset variables leave Compose settings unlocked. Connection reuse defaults to off; when enabled, each official origin permits 64 connections with 16 idle connections and a 30-second keepalive expiry. Authentication is request-scoped, upstream cookies are not stored, and shutdown closes the pools. Proxy environment, timeouts and replay rules are unchanged; disable and restart to restore fresh connections.

The account limit defaults to `0`. Positive limits skip full accounts within existing routing and free-first rules; a full free tier never spills into paid accounts. No capacity returns `503 / credential_concurrency_limit` with `Retry-After: 3`, without queueing or penalizing the account. Completion, cancellation and failed-account rotation release capacity. The credentials API exposes `in_flight` and `max_in_flight`. Only the three client generation endpoints count; limits are per process, not shared between instances. Setting `0` restores unlimited account capacity without interrupting active requests.

Before downgrading the source, remove the new CLI arguments and restore a control-store backup without these keys; disabling the features does not remove persisted settings.


### Request context

Generation responses carry a gateway-generated `X-Request-ID` for correlation with text logs and available audit details. Response-body and tool-call IDs are unchanged; client request IDs are not trusted or used for deduplication.

`request_context_mode` defaults to `legacy`, preserving existing session keys and upstream headers. Enable `scoped` in WebUI settings, with `--request-context-mode scoped`, or through `CODEBUDDY2API_REQUEST_CONTEXT_MODE=scoped`. Each client HTTP request keeps one root ID across existing retries/failover, with a new ID/span for every upstream attempt and account-isolated conversation IDs. This does not enable additional retries, server-side history or automatic cache keys.

In scoped mode, optionally send `X-Codebuddy-Session-ID`, `metadata.conversation_id` / `metadata.conversationId`, or top-level `conversation_id` / `conversationId`. Values must agree; conflicting, non-string, control-character or over-512-UTF-8-byte values return 400. Empty values fall back to a fingerprint of adapted instructions and the first user input, including image references; URLs are not fetched. Without reliable input a temporary session is used. Identical inputs without explicit IDs remain indistinguishable; `user`, `metadata.user_id` and `prompt_cache_key` are not session IDs. Raw hints are neither logged nor forwarded upstream.

Switch back to `legacy` to restore old behavior for new requests; in-flight requests retain their initial mode. Before source downgrade, remove the new startup option and restore a compatible control-store backup without `request_context_mode`.


## APIs and authentication

| Client endpoint | Description |
|-----------------|-------------|
| `POST /v1/chat/completions` | OpenAI Chat Completions |
| `POST /v1/responses` | OpenAI Responses |
| `POST /v1/messages` | Anthropic Messages |
| `POST /v1/messages/count_tokens` | Compatibility stub; currently returns `{"input_tokens":0}` without counting tokens |
| `GET /v1/models` | Available models and multipliers |
| `GET /v1/dashboard/billing/subscription` | Converted credit totals; `codebuddy_balance_usd` is the remaining balance |
| `GET /v1/dashboard/billing/usage` | `total_usage` in cents and daily breakdowns |

`hard_limit_usd` is the converted sum of remaining and used credits, not the remaining balance. Without a date-range filter, balance equals `hard_limit_usd - total_usage / 100`. These are local conversions, not a redistribution billing system.

| Management/shared endpoint | Description |
|----------------------------|-------------|
| `GET /health` | Public liveness only: `{"status":"ok"}` |
| `GET /admin/credentials` | Credential inventory and runtime state |
| `POST /admin/credentials` | Import an `.info` file from the server's controlled directory |
| `DELETE /admin/credentials/{name}` | Delete the credential file by filename; returns 409 while referenced by model bindings |
| `PATCH /admin/credentials/{id}` | One Boolean field per account: `enabled`, `auto_checkin`, or `auto_travel`; automation preference saves do not claim immediately |
| `POST /admin/oauth/start` · `GET /admin/oauth/poll` | Start/poll login; `site=cn` (default), `intl` (international WorkBuddy) or `intl-codebuddy` (international CodeBuddy) |
| `GET /admin/credits` · `POST /admin/checkin` | Inspect credits; daily-idempotent check-in followed by domestic travel when enabled |
| `POST /admin/sync` | Synchronize all enabled accounts' balances, catalogs and usage; no check-in or trial claims |
| `POST /admin/credentials/{id}/{action}` | Single-account `refresh`, `checkin`, `sync`, `travel-status` (query only), `travel` (claim then dispatch), or `trial` (one-time trial credits) |

Travel results include `phase`, optional safe `error_kind`/`http_status`/`code`, and snapshot `remaining_seconds`. `claimed`/`departed` remain true for confirmed writes even if a later query sets `ok=false` and `stale=true`; query status before another attempt.

Pages use `/dashboard/*`, management APIs use `/admin/*`, and clients retain `/v1/*`. `/cn` and `/intl` API prefixes are not registered. Automatic model routing requires no client URL changes.

Management requires an API key. The WebUI exchanges that key for an HttpOnly management Cookie, which only authorizes `/admin/*`, not `/v1/*`. API clients send `Authorization: Bearer <key>` or `X-Api-Key`. An empty key preserves legacy unauthenticated inference only, not management. `/health` never exposes account, path or exception details.

### Management Origin / CSRF switch

Enabled by default. When OAuth polling omits both `Origin` and `Sec-Fetch-Site`, a same-origin `Referer` (matching scheme, host and port) is accepted, but a valid CSRF token is still required. An existing `Origin` takes precedence; without it, supplied Fetch Metadata must be `same-origin` and cannot fall back to Referer. Login and writes still require Origin.

Normal same-origin access does not require disabling protection. If errors persist, use a consistent access URL, check the proxy's forwarded Host/scheme, and refresh the page and log in again. Only for trusted local deployments, append `--admin-csrf false` to the startup command or set this in your existing `.env`:

```dotenv
CODEBUDDY2API_ADMIN_CSRF=false
```

This is startup-only, CLI takes precedence over the environment, and the WebUI cannot change it. Use a source/image build containing this option and the updated Compose configuration. Recreate the container after changing its environment; `docker compose restart` alone is insufficient. Adding the variable does not add this feature to an older image.

Disabling skips login Origin checks and Origin/CSRF checks on Cookie-authenticated management writes and OAuth polling. API keys, session expiry, OAuth task ownership, official authorization-site validation and dangerous-action confirmations remain enforced; `/v1/*` is unaffected.

**Disabling weakens browser cross-site request protection; do not expose this configuration directly to the public Internet.** Set `--admin-csrf true` or the environment value to `true`, then restart to re-enable protection.

### Server-side path imports

The WebUI supports direct uploads; these rules concern path imports through `POST /admin/credentials`:

- Place files in `auth/imports/` or the server directory set by `CODEBUDDY_IMPORT_DIR`.
- Only regular `.info` files directly inside that directory are accepted; symlinks, subdirectories and files over 1 MiB are rejected.
- Send `{"path":"account.info"}` or that file's absolute path. The same filename is updated under the import rules.
- Identity includes product profile, UID and tenant. If another file already owns that identity, import returns 409; the same UID can coexist across products or tenants. Deletion takes a filename, while enable/disable takes an identity ID.

## Models and scheduling

Select client models from the WebUI or `GET /v1/models`. Catalogs are cached by account/tenant, region, product and client version in `auth/model-catalog.json`, with a default 6-hour TTL. New credentials trigger synchronization; failures retain only the same account's trusted cache. Legacy unscoped catalogs cannot authorize other accounts.

Each `/v3/config` refresh caches the agent picker subset as `models` and the account root table
as `serves`. Routing and `GET /v1/models` merge these candidates, with picker metadata winning
for duplicate IDs. Both scopes retain `disabled` and `availableModels` filtering; models without
tool support are excluded.

The root table is not a guarantee that a backend serves every listed model; measured `11102`
avoidance still applies. Unknown account catalogs do not authorize dispatch and must not trigger
a premature all-backends-unsupported 404; they retain the retryable readiness state.
Legacy cache entries without `serves` use the picker until the next refresh.

Beyond standard model fields, `credits` is the lowest source multiplier: `0.0` identifies a zero-multiplier source and `null` means no parseable multiplier was declared. `credits_by_profile` provides source details, such as `{"intl-work":0.0,"cn-cli":0.03}`. Compatible clients may ignore these fields; multipliers are not guaranteed to stay unchanged.

Credential domain / token issuer determine the product identity. Chat and refresh use fixed origins with separate product headers:

| Profile | Chat / refresh origin |
|---------|-----------------------|
| `cn-cli` | `https://copilot.tencent.com` |
| `cn-work` | `https://www.workbuddy.cn` |
| `intl-cli` | `https://www.codebuddy.ai` |
| `intl-work` | `https://www.workbuddy.ai` |

- By default, accounts are selected only for models supported by their own trusted catalog
  (picker ∪ account root table, see above); catalogs and balances are never borrowed across accounts. Concrete zero-multiplier models take priority, followed by expiring-credit priority, cooldowns and session stickiness.
- Zero-balance accounts leave paid-model rotation but can still serve concrete zero-multiplier models declared by their own catalog; they rejoin once balance recovers. International paid models need a known positive balance, with an exception for concrete zero-multiplier models.
- `auto` schedules an account's default, not any model. International accounts need positive balance and `default-model` in their catalog; domestic WorkBuddy must declare `auto`, and domestic CLI needs a known nonempty usable catalog. `auto` does not receive the concrete zero-multiplier balance exemption.
- WebUI region, product and credential bindings strictly limit candidates; unavailable bindings never fall back to unselected accounts. Disabled models also reject direct requests. Renaming hides the original ID unless you choose to retain it.
- Sent requests are not replayed against another account because of account availability or HTTP errors; later requests select again. Pending catalog/credential readiness usually returns 503 with `Retry-After`; unsupported or disabled models return 404.

## Request boundaries

- All three generation protocols normalize `developer` to `system`, move an existing system message first or insert a default. This normalization does not mutate the caller's payload. Responses projection and optional desensitization process content separately; the whole pipeline is not a verbatim pass-through.
- Images count across all history and tool results, including duplicates, in message/content array order. The default keeps the newest 16, removing only excess images while retaining text and message structure; emptied image content receives a text placeholder.
- `--image-policy error` returns local `413 / too_many_images`. JSON still over budget after processing returns `413 / request_too_large`, without further text truncation to fit the limit.
- Image count does not guarantee acceptable individual image sizes or model vision support. URL/base64 images can be converted; Responses image `file_id` is unsupported.
- When `stream` is omitted all three endpoints follow the protocol default and return a complete JSON response; `stream` must be a boolean. Streaming Responses and Chat/Messages with tools aggregate and validate before emitting SSE; not every path forwards tokens in real time.
- Inference errors follow the client protocol: OpenAI routes return a top-level `error` object and Messages returns `{"type": "error", ...}`. Status codes are retained; errors after streaming starts are reported through SSE without replay.
- Valid upstream `Retry-After` values (0–86400 seconds or equivalent HTTP dates) are returned as seconds before streaming starts; 429 only cools the selected account/model. Invalid or expired values fall back to the body's reset time or 600 seconds. Pool-generated 429 responses include the remaining wait.
- Chat and Responses preserve an explicit client `prompt_cache_key` without generating one; cache hits and savings depend on the upstream.
- Unsupported capabilities are rejected rather than silently degraded: chat `n` other than 1 and the Responses state fields `previous_response_id`/`conversation` (this gateway keeps no server-side response state) return 400; length-truncated or content-filtered Responses are reported as `incomplete`, never disguised as `completed`.
- `/v1/messages/count_tokens` returns a character-based heuristic estimate for budgeting, not an exact count.
- Text logs and SQLite auditing have separate budgets. Logs contain bounded, redacted previews, not complete original requests. Treat logs, credential exports and backups as private data.

## Deployment exposure and credential intake

- The compose port mapping binds loopback by default (`CODEBUDDY2API_BIND` defaults to 127.0.0.1); after resolving CLI, environment and saved settings, a native non-loopback bind with an empty effective API key refuses to start unless `CODEBUDDY2API_ALLOW_OPEN_NOAUTH=true` is set explicitly.
- When a key is configured, generation and token-count POSTs verify request headers before buffering bodies or reserving inference capacity; invalid keys return 401 even while generation slots are full. Other routes retain their existing authentication and routing behavior.
- Credential imports/uploads persist the normalized form (token aliases folded into the canonical fields); strict JSON parsing rejects NaN/Infinity, and `expiresAt`/`lastRefreshTime` must be plausible finite millisecond timestamps.

## Billing data integrity

- Balances and usage are paginated in full; when a page cap is hit or an account's sync fails, responses carry `partial: true` (and `stale_accounts`) instead of pretending to be exact.
- A failed account keeps its last good snapshot; HTTP 200 responses with a failing business code or missing structure are treated as errors and never overwrite history.
- If every account fails before a first snapshot, both billing endpoints still report partial data and stale accounts; quota-delta fallback remains in use. Existing account snapshots retain their original fetch time on failures.
- daily_costs are priced per site per day at that site's price, not at one blended average.

## Troubleshooting and retries

| Symptom | Behavior / action |
|---------|-------------------|
| Cannot sign in to WebUI | Configure an API key; sign in and restart unfinished OAuth after changing it |
| Local 401 | Client key differs from the gateway key |
| Upstream 401 / 403 | Credential-level authentication circuit opens; inspect and log in again in the WebUI |
| 429 | Cool down that upstream model on the credential; later requests rebind automatically. All candidates cooling down still returns 429; with `--failover-max` the in-flight request is replayed on another credential instead |
| Upstream `service info not found` (code 11102) | That backend does not serve the model at all: avoid it for the `(backend, model)` pair, route the model to another backend, and return 404 when none has it. Half-open after 6 h, exponential backoff up to 24 h, cleared at once by one successful call; inspect via `GET /admin/model-blocks` |
| Connection setup failure | Retry once on a fresh connection: `ConnectError` and `ConnectTimeout` fail before the first body byte, so the upstream holds nothing and replaying cannot double-bill |
| Post-send disconnect, read timeout or protocol error | No network replay, avoiding duplicate billing; logs include exception type and elapsed time |
| Client hangs up before a non-streaming response is ready | The upstream call is cancelled and its concurrency slot returned at once; the request is audited as `cancelled`, never as a completed answer. Streaming already behaves this way |
| Streaming request fails before the first byte | Reported with the real HTTP status, exactly like `stream=false`. A 200 carrying only an in-band `error` event is read by clients as an empty answer, so the session ends silently while the audit log records a success |
| Credential failover (`--failover-max`) | Off by default. When enabled, a failure that happened before any byte reached the client is retried on another credential, up to N times, and is audited as `success` with a `failover_recovered` attempt marker. Only upstream HTTP rejections (401/403/429/502/503/504) and request bodies the upstream provably never started receiving (`ConnectError`/`ConnectTimeout`) qualify: content-filter rejections, 502s synthesized from an already-open stream, read timeouts and protocol errors are never replayed, and if no other credential is available the original status is surfaced — **Billing**: 401/403/429/503 and those transport failures happen at admission time and cannot be billed; a 502/504 may already have been processed and billed upstream, but its result never reached the client, so refusing to replay it recovers no credit — it only turns a paid-for attempt into a broken session. Such replays are tagged `上游可能已处理该请求` in the log for reconciliation |
| Write-timeout replay (`--retry-write-timeout`) | Off by default. A write timeout proves the declared body was not fully sent, not that the upstream ignored the bytes it did receive, so it is excluded from both the connect retry and credential failover until explicitly enabled. Long cross-border sessions fail here more often than in the handshake, so operators who have confirmed their upstream does not bill partial bodies can turn this on; those replays carry the same `上游可能已处理该请求` log tag |
| Malformed tool calls | Aggregate validation permits up to `--tool-call-max-retry` (default 3) additional generations, each consuming credits and recorded with its usage in the attempt details; exhaustion returns an error |
| Empty or truncated upstream stream | No valid output, a missing end marker or an error is not reported as success |
| Content-filter rejection | With desensitization and `--no-compact`, a complete non-streaming filter-only rejection may receive one shorter-template retry on the same account. No streaming filter retry, circuit opening or account rotation |
| Slow responses | Inspect timing and failed attempts in the WebUI, then choose a faster model supported by the account |
| Same account invalidated elsewhere | Independent desktop/gateway refreshes may invalidate each other; prefer separate browser login or stop using the other client |
