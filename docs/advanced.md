# Advanced reference

[Home](../README.md) · [简体中文](advanced.zh-CN.md)

Use the [WebUI](webui.md) for everyday management. See [deployment](deployment.md) for startup methods and [client configuration](clients.md) for examples.

## Configuration and CLI

Precedence: **explicit CLI flags > process environment > `.env` > saved SQLite settings > defaults**. Hot settings apply immediately; restart-marked settings require a manual restart. Change locked options at their source; the WebUI does not edit `.env` or expose API keys.

Compose explicitly passes some environment variables and CLI flags, so deleting a line from `.env` may not unlock it. Recreate the container after changing these values; to let the WebUI manage them, also remove the corresponding explicit Compose settings.

| Flag | Default | Description |
|------|---------|-------------|
| `--host` / `--port` | `127.0.0.1` / `8787` | Local listener |
| `--api-key` | saved default | Shared management/inference key; first local interactive startup generates and saves one if absent; explicit empty locks management |
| `--admin-csrf [true/false]` | `true` | Startup-only management Origin/CSRF checks; disabling does not bypass API-key or session authentication |
| `--admin-allowed-origins` | none | Extra trusted management Origins (comma-separated; bare domains mean HTTPS) for reverse-proxy sign-in; hot and WebUI-editable |
| `--auth-file` | scan `auth/` | Explicit credential file, repeatable; disables scanning other files |
| `--log` | retired | Warns without writing a file; use SQLite logs in the WebUI |
| `--desensitize` | off | Adapt fixed CLI templates, compact runtime prompts and mask keywords with zero-width characters |
| `--no-compact` | off | With desensitization, retain fuller instructions while adapting templates and pruning runtime context; does not disable Responses projection |
| `--keep-tool-metadata [true/false]` | `false` | Retain tool descriptions and parameter-schema `description/title`, independently of prompt compaction |
| `--skip-check` | off | Skip startup preflight |
| `--credit-price-cny` | `0.014` | Domestic CNY per credit for billing conversion |
| `--credit-price-usd` | `0.03` | International USD per credit for billing conversion |
| `--usd-rate` | `7.15` | CNY per USD for billing conversion |
| `--model-catalog-ttl` | `21600` | Model catalog cache TTL, seconds |
| `--no-model-guard` | off | Disable the out-of-catalog guard; passthrough is limited to one product profile and still respects disabling, bindings and catalog readiness |
| `--model-capability-guard [true/false]` | `true` | Preflight declared image, tool, reasoning and mapped output limits; changes affect new requests |
| `--max-images` | `16` | Total images per request; `0` permits no images |
| `--image-policy` | `truncate` | Keep newest images; `error` rejects excess images with 413 |
| `--responses-projection-mode balanced\|passthrough` | `balanced` | Rewrite recognized harness blocks that have stable summaries; passthrough disables Responses projection |
| `--responses-projection-max-bytes` | `40000` | Complete per-item UTF-8 limit for assistant text, tool-argument JSON and tool results; `0` disables, otherwise valid range is `256..33554432` |
| `--tool-call-max-retry` | `3` | Extra generations after malformed tool calls (each consumes credits); `0` disables retries |
| `--max-inbound-bytes` | `67108864` | Raw body limit for generation and token-count POSTs, before parsing (chunked included); other routes are not buffered; 413 beyond it |
| `--max-collect-bytes` | `8388608` | Total retained-output budget for aggregation and realtime validation (content + reasoning + tool arguments/metadata); `response_too_large` beyond it; `0` disables |
| `--stream-mode compatible\|realtime` | `compatible` | `compatible` preserves aggregate/replay behavior; `realtime` incrementally streams all three protocols and does not regenerate tool arguments |
| `--max-concurrent` | `64` | Concurrency limit for the three generation endpoints only; excess requests get 503 with Retry-After; token counting is unaffected; `0` disables |
| `--max-inflight-per-account` | `0` | Per-process, per-account in-flight client inference limit; `0` disables, full accounts return 503 |
| `--upstream-keepalive [true/false]` | `false` | Bounded connection reuse isolated by official origin; requires restart |
| `--request-context-mode` | `legacy` | `scoped` enables explicit sessions and per-attempt tracing; changes apply to new requests |
| `--failover-max` | `0` | Extra credentials tried when a request fails before the first response byte reaches the client; `0` keeps the upstream behaviour of surfacing the failure directly |
| `--retry-write-timeout` | `false` | Opt a request-body write timeout into replay (fresh connection and `--failover-max`), accepting that bytes already sent may have been processed |
| `--max-request-bytes` | `33554432` | Positive byte limit for processed upstream JSON, excluding gateway-only metadata |
| `--log-body-limit` | `65536` | Legacy text-preview option; text output is retired and SQLite diagnostics use their own budget |

Environment variables include `CODEBUDDY_AUTH_DIR`, `CODEBUDDY_IMPORT_DIR`, `CODEBUDDY2API_KEY`, `CODEBUDDY2API_ADMIN_CSRF`, `CODEBUDDY2API_ADMIN_ORIGINS`, `CODEBUDDY2API_KEEP_TOOL_METADATA`, `CODEBUDDY2API_STREAM_MODE`, `CODEBUDDY2API_LOG`, `CODEBUDDY2API_RESPONSES_PROJECTION_MODE`, `CODEBUDDY2API_RESPONSES_PROJECTION_MAX_BYTES`, `CODEBUDDY2API_MAX_IMAGES`, `CODEBUDDY2API_IMAGE_POLICY`, `CODEBUDDY2API_MAX_REQUEST_BYTES`, `CODEBUDDY2API_LOG_BODY_LIMIT`, `CODEBUDDY2API_FAILOVER_MAX` and `CODEBUDDY2API_RETRY_WRITE_TIMEOUT`. See [deployment](deployment.md) for startup examples.

### Responses projection

Configure these hot settings through the WebUI, CLI, process environment or `.env`. Precedence is CLI > process environment > `.env` > saved SQLite value > default; CLI/environment values lock the WebUI fields. Compose forwards a variable only when the host environment sets it, so leaving both unset keeps the WebUI editable.

`responses_projection_mode` defaults to `balanced` and accepts only `balanced` or `passthrough`. Balanced mode rewrites only recognized harness blocks that have stable summaries; text outside those blocks is not budgeted or truncated. It also applies Codex-style head/tail truncation to generated assistant content, complete tool-argument JSON and tool results according to `responses_projection_max_bytes`. Text markers report original bytes, estimated tokens and total lines. Oversized tool arguments remain valid JSON and use a bounded object containing the original head, tail and size metadata. Passthrough disables Responses projection completely.

`responses_projection_max_bytes` defaults to `40000`; valid values are `0` or `256..33554432`. `0` disables per-item truncation only; global inbound/request and output gates still apply. Neither setting changes the client Base URL.

Downgrading to source that predates these keys requires the offline removal of `settings.responses_projection_mode` and `settings.responses_projection_max_bytes` described under [Streaming modes](#streaming-modes).

### Tool metadata retention

Responses projection no longer changes tool definitions or schemas. When desensitization is enabled, it strips tool descriptions and string `description/title` annotations by default; enabling this setting retains and processes that text across Chat, Responses and Messages. `--no-compact` does not change this setting.

- **WebUI:** Settings → Keep tool descriptions; unlocked changes apply immediately and persist.
- **CLI:** append `--keep-tool-metadata` or `--keep-tool-metadata true` to the existing command; explicit `false` overrides the environment.
- **Environment:** set `CODEBUDDY2API_KEEP_TOOL_METADATA=true`. Compose passes it only when set, leaving the WebUI unlocked otherwise. Remove or comment out the variable to remove the environment lock; do not set an empty string.

Use a source/image build and Compose configuration containing this feature; recreate containers after changing its environment. Retained descriptions may increase input tokens and content-filter risk; set `false` to restore the desensitization default. This setting does not change Responses balanced/passthrough mode or relax request-size limits.

### Streaming modes

`stream_mode` defaults to `compatible`. Configure it through `--stream-mode compatible|realtime`, `CODEBUDDY2API_STREAM_MODE`, or the WebUI enum; explicit CLI/environment sources lock that field. Every generation request, including non-streaming, records the selected mode and freezes it with `max_collect_bytes` before routing. Hot changes affect only later requests, not in-flight failovers. Non-streaming responses remain aggregated JSON; there is no client mode override.

- `compatible` preserves existing behavior: Responses streams aggregate first; Chat and Messages aggregate when tools are present and otherwise pass through upstream increments. Aggregated output is validated and replayed in fragments. Non-stream requests always use the validated aggregate path in either mode.
- `realtime` forwards reasoning, text, refusal and tool-argument increments for all three protocols. Responses assigns stable indexes when items start; Anthropic uses stable block indexes. Adapters buffer tools with missing identity until the argument phase or terminal marker, append metadata fragments without guessing from prefixes, and reject identity changes after an item starts. `max_collect_bytes` bounds retained UTF-8 output; `0` disables that limit.

Realtime Messages keeps one content block open at a time. The active tool remains incremental; later tool, text or thinking blocks may wait until upstream completion. Deferred event bytes share `max_collect_bytes`. A tool still awaiting its identity does not block unrelated text before its block starts.

Realtime mode never regenerates malformed or incomplete tool arguments. Tool IDs, names, declared names, JSON-object arguments and `tool_choice` are checked at the terminal boundary before a success terminal is sent. In realtime, a tool-bearing completion must also carry the upstream `tool_calls` finish marker; a `stop` marker with tool calls is rejected, while compatible mode keeps its legacy acceptance behavior. Before any downstream byte, failures retain the upstream HTTP error and existing bounded pre-response failover rules. After any byte, malformed tools, disconnects, stream errors and budget overflow produce a protocol error terminal without credential replay or switching; valid `length`, refusal and content-filter results keep their native protocol distinctions (Responses reports truncation/filtering as `incomplete`, never `completed`) and are not regenerated. Clients must therefore accept partial output followed by an error rather than assuming every opened SSE stream completes successfully. Audit records retain only an allowlisted `stream_mode` marker plus available upstream usage.

Explicit filter-only terminals are valid even without output: realtime Responses emits `response.incomplete` with `content_filter`, retaining available usage. This does not permit an ordinary empty response, missing terminal or error frame to succeed.

For runtime fallback, select `compatible` to restore aggregate streaming and tool-argument repair without changing saved state. Before running older source, remove the new CLI/environment option, stop the gateway and back up the **current** data directory, including its SQLite/WAL/SHM generation. Do not restore a stale pre-upgrade database: that could roll back newer claims, sessions, revocations and account state. The following offline procedure deletes only the listed `settings.<name>` keys and increments the revision, then checks integrity; all other settings and tables remain intact. Pass one key or several. Never run it against a live database or mix SQLite generations.

```sh
python3 - /path/to/control.sqlite3 settings.stream_mode <<'PY'
import json, sqlite3, sys
path, names = sys.argv[1], sys.argv[2:]
keys = [name.split(".", 1)[1] for name in names if name.count(".") == 1 and name.split(".", 1)[1]]
if len(keys) != len(names):
    raise SystemExit("pass one or more settings.<name> keys")
con = sqlite3.connect(path)
try:
    con.execute("BEGIN IMMEDIATE")
    row = con.execute("SELECT revision,payload FROM control WHERE id=1").fetchone()
    if row is None:
        raise SystemExit("missing control row")
    revision, payload = row
    data = json.loads(payload)
    if set(data) != {"settings", "models", "credentials"} or not isinstance(data["settings"], dict):
        raise SystemExit("unexpected control payload")
    present = [key for key in keys if key in data["settings"]]
    if not present:
        raise SystemExit("no requested key is present; no write needed")
    for key in present:
        del data["settings"][key]
    con.execute("UPDATE control SET revision=?,payload=? WHERE id=1",
                (revision + 1, json.dumps(data, ensure_ascii=False, allow_nan=False)))
    con.commit()
    if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise SystemExit("integrity check failed")
finally:
    con.close()
print("ok")
PY
```

Older strict setting validators reject the unknown saved key, so changing only its value does not make old source compatible. This procedure is intentionally offline and operator-scoped; do not perform it on production without a current backup and a stopped service.

### Connection reuse and account capacity

Both settings are available in the WebUI; their environment variables are `CODEBUDDY2API_UPSTREAM_KEEPALIVE` and `CODEBUDDY2API_MAX_INFLIGHT_PER_ACCOUNT`. Unset variables leave Compose settings unlocked. Connection reuse defaults to off; when enabled, each official origin permits 64 connections with 16 idle connections and a 30-second keepalive expiry. Authentication is request-scoped, upstream cookies are not stored, and shutdown closes the pools. Proxy environment, timeouts and replay rules are unchanged; disable and restart to restore fresh connections.

The account limit defaults to `0`. Positive limits skip full accounts within existing routing and free-first rules; a full free tier never spills into paid accounts. No capacity returns `503 / credential_concurrency_limit` with `Retry-After: 3`, without queueing or penalizing the account. Completion, cancellation and failed-account rotation release capacity. The credentials API exposes `in_flight` and `max_in_flight`. Only the three client generation endpoints count; limits are per process, not shared between instances. Setting `0` restores unlimited account capacity without interrupting active requests.

### Request context

Generation responses carry a gateway-generated `X-Request-ID` for correlation with text logs and available audit details. Response-body and tool-call IDs are unchanged; client request IDs are not trusted or used for deduplication.

`request_context_mode` defaults to `legacy`, preserving existing session keys and upstream headers. Enable `scoped` in WebUI settings, with `--request-context-mode scoped`, or through `CODEBUDDY2API_REQUEST_CONTEXT_MODE=scoped`. Each client HTTP request keeps one root ID across existing retries/failover, with a new ID/span for every upstream attempt and account-isolated conversation IDs. This does not enable additional retries, server-side history or automatic cache keys.

In scoped mode, optionally send `X-Codebuddy-Session-ID`, `metadata.conversation_id` / `metadata.conversationId`, or top-level `conversation_id` / `conversationId`. Values must agree; conflicting, non-string, control-character or over-512-UTF-8-byte values return 400. Empty values fall back to a fingerprint of adapted instructions and the first user input, including image references; URLs are not fetched. Without reliable input a temporary session is used. Identical inputs without explicit IDs remain indistinguishable; `user`, `metadata.user_id` and `prompt_cache_key` are not session IDs. Raw hints are neither logged nor forwarded upstream.

Switch back to `legacy` to restore old behavior for new requests; in-flight requests retain their initial mode.

## Automation and rewards

Automatic check-in and Buddy travel are per-account switches (WebUI credentials page): on by default domestically, off internationally, applied live without restart.

`CODEBUDDY2API_AUTO_ACCEPT_BUDDY` is startup-only and defaults to `false`. It preauthorizes enabled domestic accounts for first-Buddy onboarding, agreement and travel; automatic travel still respects its account switch. `first_buddy` needs no acceptance API: pending states, including `not_accepted`, allow one real domestic WorkBuddy conversation on that account. Prefer an eligible zero-rate model, otherwise the lowest known rate; request at most 32 output tokens with possible credit usage. Other reward tasks, paid boxes, pet switching and international trials are excluded.

Manual `POST /admin/credentials/{id}/travel` returns `buddy_confirmation` with official terms and a separate `authorization` scope. Submit `{"confirm_buddy":true,"agreement_revision":"<returned revision>"}` after consent; old adoption-only revisions are rejected. `can_claim` describes eligibility and never disables consent.

`control.sqlite3` preserves consent and one actual onboarding conversation per account across restarts. A live preflight cancellation releases only its own unsent reservation; unknown or sent attempts are never released automatically. Historical acceptance records do not block an unsent conversation. Only official task completion permits adoption. Unconfirmed first-claim sends remain reserved beyond 24 hours and only reconcile through reads; pre-claim failures may resume after backoff. Keep this database when upgrading; travel-status and balance sync remain read-only.

Travel claims and departures share an account-scoped write reservation. Uncertain results do not expire or replay; fresh status reads reconcile them without issuing upstream writes. Store failures stop claims and departures, and local readback updates preserve receipt ownership across processes.

Trial credits are manual-only for eligible `intl-work` accounts through the credential drawer or `POST /admin/credentials/{id}/trial`; startup and maintenance never claim. Safe results, successful claims and reservations persist in `control.sqlite3`; failed attempts retain the 24-hour backoff. Response bodies are capped at 64 KiB and never returned to the browser. Preserve the database when upgrading.

`CODEBUDDY2API_AUTO_TRIAL` and `--auto-trial` are retired: old startup options warn and do nothing; saved Boolean `auto_trial` settings are ignored on load. Remove them from deployment configuration. Before reverting to older code, check these old settings to avoid re-enabling automatic claims.

## APIs and authentication

| Client endpoint | Description |
|-----------------|-------------|
| `POST /v1/chat/completions` | OpenAI Chat Completions |
| `POST /v1/responses` | OpenAI Responses |
| `POST /v1/messages` | Anthropic Messages |
| `POST /v1/messages/count_tokens` | Character-based heuristic token estimate for budgeting, not an exact count |
| `GET /v1/models` | Available models, multipliers and safe per-profile declarations |
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
| `POST /admin/credentials/{id}/{action}` | Single-account `refresh`, `checkin`, `sync`, `travel-status` (query only), `travel` (claim then dispatch), `trial` (one-time trial credits), or `reset-cooldown` (local-only) |

Travel results include `phase`, optional safe `error_kind`/`http_status`/`code`, and snapshot `remaining_seconds`. `claimed`/`departed` remain true for confirmed writes even if a later query sets `ok=false` and `stale=true`; query status before another attempt.

`reset-cooldown` takes no request body and lifts every cooldown held by one account — both its 401/403 circuit breaker and its per-model 429 cooldowns — so an operator can recover from a cooldown recorded in error without restarting the gateway. It only edits local state: it never refreshes a token, contacts upstream, or queues synchronization, and it works for a manually disabled account. The result separates `changed_in_memory` from `durable`; `ok` is false when the write failed, in which case the in-memory reset is already effective but a restart would restore the stored row, and the request can simply be repeated.

Pages use `/dashboard/*`, management APIs use `/admin/*`, and clients retain `/v1/*`. `/cn` and `/intl` API prefixes are not registered. Automatic model routing requires no client URL changes.

Management requires an API key. The WebUI exchanges that key for an HttpOnly management Cookie, which only authorizes `/admin/*`, not `/v1/*`. API clients send `Authorization: Bearer <key>` or `X-Api-Key`. An empty key preserves legacy unauthenticated inference only, not management. `/health` never exposes account, path or exception details.

### Management Origin / CSRF switch

Enabled by default. When OAuth polling omits both `Origin` and `Sec-Fetch-Site`, a same-origin `Referer` (matching scheme, host and port) is accepted, but a valid CSRF token is still required. An existing `Origin` takes precedence; without it, supplied Fetch Metadata must be `same-origin` and cannot fall back to Referer. Login and writes still require Origin.

Normal same-origin access does not require disabling protection. Behind a reverse proxy that rewrites the forwarded Host/scheme (for example HTTPS on a bound domain while the container sees HTTP), the browser Origin no longer matches what the server sees and login fails Origin checks. Add the public address to `admin_allowed_origins` (WebUI system settings, hot) or set `CODEBUDDY2API_ADMIN_ORIGINS` / `--admin-allowed-origins`: comma separated origins or bare domains (`https://chat.example.com`, `chat.example.com`; bare domains mean HTTPS), up to 32 entries. An explicit CLI or environment value locks the WebUI field. Prefer this over disabling protection; if errors persist, use a consistent access URL, check the proxy's forwarded Host/scheme, and refresh the page and log in again. Only for trusted local deployments, append `--admin-csrf false` to the startup command or set this in your existing `.env`:

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

Select client models from the WebUI or `GET /v1/models`. Raw catalogs remain cached by account/tenant, region, product and client version in `auth/control.sqlite3`, with a default 6-hour TTL. New credentials trigger synchronization; failed refreshes retain that account's trusted cache. Legacy unscoped caches do not become international sharing sources.

International CLI and WorkBuddy use a deduplicated shared view from enabled, catalog-ready international accounts. A target account must have its own synchronized catalog; its existing model declarations win unchanged. Missing IDs inherit shared declarations, retaining `catalog_source` and safe `source_variants`. Conflicting inherited rates use the higher known rate, limits the smaller known value, reasoning options their intersection and differing descriptive fields are omitted; unknown prices never mean free. Domestic catalogs, credentials, balances, bindings and `auto` defaults remain independent. Shared rates are catalog references, not billing or permission guarantees.

Each `/v3/config` refresh caches the agent picker subset as `models` and the account root table as `serves`. Routing and `GET /v1/models` merge these candidates, with picker metadata winning for duplicate IDs. Both scopes retain `disabled` and `availableModels` filtering; models without tool support are excluded.

The root table is not a guarantee that a backend serves every listed model; measured `11102` avoidance still applies. Unknown account catalogs do not authorize dispatch and must not trigger a premature all-backends-unsupported 404; they retain the retryable readiness state. Legacy cache entries without `serves` use the picker until the next refresh.

Beyond standard model fields, `credits` is the lowest source multiplier: `0.0` identifies a zero-multiplier source and `null` means no parseable multiplier was declared. `credits_by_profile` provides source details, such as `{"intl-work":0.0,"cn-cli":0.03}`. Compatible clients may ignore these fields; multipliers are not guaranteed to stay unchanged.

Credential domain / token issuer determine the product identity. Chat and refresh use fixed origins with separate product headers:

| Profile | Chat / refresh origin |
|---------|-----------------------|
| `cn-cli` | `https://copilot.tencent.com` |
| `cn-work` | `https://www.workbuddy.cn` |
| `intl-cli` | `https://www.codebuddy.ai` |
| `intl-work` | `https://www.workbuddy.ai` |

- Domestic accounts use their own trusted catalogs; international accounts use the shared view above. Concrete zero-rate models take priority, followed by credit expiry, cooldowns and session stickiness. Balances are never borrowed.
- Zero-balance accounts leave paid-model rotation but may use concrete zero-rate models in their effective catalog; they rejoin once balance recovers. International paid models require a known positive balance.
- `auto` schedules an account's default, not any model. International accounts need positive balance and `default-model` in their catalog; domestic WorkBuddy must declare `auto`, and domestic CLI needs a known nonempty usable catalog. `auto` does not receive the concrete zero-multiplier balance exemption.
- WebUI region, product and credential bindings strictly limit candidates; unavailable bindings never fall back to unselected accounts. Disabled models also reject direct requests. Renaming hides the original ID unless you choose to retain it.
- Sent requests are not replayed against another account because of account availability or HTTP errors; later requests select again. Pending catalog/credential readiness usually returns 503 with `Retry-After`; unsupported or disabled models return 404.

### Model declarations and image compatibility

`/v1/models` retains its existing fields and adds `capabilities`, `limits` and `metadata_by_profile`; management and routing previews expose the same metadata. Capabilities use `supported`, `unsupported`, `mixed` or `unknown`. Limits carry `state` (`known`, `mixed`, `unknown`) and `value`; only unanimous known limits have a numeric value. Per-profile arrays preserve distinct account declarations without identities. Safe descriptions, capabilities, windows, reasoning options, related models and parameter suggestions are allowlisted; credentials, internal configuration and authenticated URLs are excluded. These are upstream declarations, not native-model or measured guarantees; suggestions do not override requests.

`model_capability_guard` defaults to `true`; use WebUI settings, `--model-capability-guard false` or `CODEBUDDY2API_MODEL_CAPABILITY_GUARD=false` to disable it. Explicit mismatches return 400 before sending, within existing bindings and the current free-first tier; unknown capabilities remain compatible. Requests retain their entry-time switch. Checks cover images, tools/history, declared reasoning options and the `max_tokens` output limit (including mapped Responses `max_output_tokens`); input tokens are not estimated, `max_completion_tokens` is not renamed or checked against this limit, and Anthropic thinking budgets are not converted. Disabling this guard leaves authentication, catalog authorization, capacity and size limits intact.

Both international profiles merge image-bearing consecutive `user` runs only after routing, preserving content order and image data. Domestic bodies, text-only runs and system/assistant/tool boundaries remain unchanged. Conflicting message attributes or unrepresentable content return `400 / image_user_run_not_mergeable`; final byte limits still apply. This compatibility step remains enabled when capability preflight is disabled; it neither adds retries nor makes a text model natively visual.

## Reasoning compatibility

Messages `enabled` / `adaptive` activate Chat reasoning; `output_config.effort` and Responses `reasoning.effort` map to `reasoning_effort`. An explicit top-level `reasoning_effort` takes precedence, except Messages `disabled` always selects `none`; model capability checks still apply. Omitted controls leave upstream defaults unchanged.

Without an explicit effort, Messages activation uses the selected account's `reasoning.defaultEffort` or legacy `reasoning.effort`, restricted to its declared options. Otherwise it prefers `high`, then an available option; unknown declarations fall back to `high`. Failover resolves the replacement account's default again.

Manual `enabled` requires an integer `budget_tokens >= 1024`, but the budget is not an exact upstream token limit; `max_tokens` is forwarded unchanged. This mapping does not reproduce native adaptive scheduling. Only `display: summarized` is supported.

Readable history is kept in `reasoning_content`, never ordinary answer text. Responses uses readable `content` before `summary`; summaries cannot reconstruct native hidden reasoning. Signatures are not forwarded. `redacted_thinking`, encrypted-only thinking and non-empty Responses `encrypted_content` return 400 before routing; upstreams decide which readable history they use.

## Request boundaries

- All three generation protocols normalize `developer` to `system`, move an existing system message first or insert a default. This normalization does not mutate the caller's payload. Optional [Responses projection](#responses-projection) and desensitization process content separately; the whole pipeline is not a verbatim pass-through by default.
- Images count across all history and tool results, including duplicates, in message/content array order. The default keeps the newest 16, removing only excess images while retaining text and message structure; emptied image content receives a text placeholder.
- `--image-policy error` returns local `413 / too_many_images`. JSON still over budget after processing returns `413 / request_too_large`, without further text truncation to fit the limit.
- Image count does not guarantee acceptable individual image sizes or model vision support. URL/base64 images can be converted; Responses image `file_id` is unsupported.
- With `stream_mode=compatible` (the default), streaming Responses and Chat/Messages with tools aggregate and validate before emitting SSE; Chat/Messages without tools pass through upstream increments. `realtime` streams all three incrementally, while omitted/non-stream requests remain complete validated JSON.
- Inference errors follow the client protocol: OpenAI routes return a top-level `error` object and Messages returns `{"type": "error", ...}`. Status codes are retained; errors after streaming starts are reported through SSE without replay.
- Valid upstream `Retry-After` values (0–86400 seconds or equivalent HTTP dates) are returned as seconds before streaming starts; 429 only cools the selected account/model. Invalid or expired values fall back to the body's reset time or 600 seconds. Pool-generated 429 responses include the remaining wait.
- Chat and Responses preserve an explicit client `prompt_cache_key` without generating one; cache hits and savings depend on the upstream.
- Unsupported capabilities are rejected rather than silently degraded: chat `n` other than 1 and the Responses state fields `previous_response_id`/`conversation` (this gateway keeps no server-side response state) return 400; length-truncated or content-filtered Responses are reported as `incomplete`, never disguised as `completed`.
- SQLite auditing retains only bounded, redacted diagnostics, not complete original requests. Treat logs, credential exports and backups as private data.

## Deployment exposure and credential intake

- Compose maps loopback by default. Without a configured or saved key, `CODEBUDDY2API_ALLOW_OPEN_NOAUTH=true` explicitly permits headless/non-loopback inference without generating a key; management stays locked. The shipped image sets this compatibility opt-in, so configure `CODEBUDDY2API_KEY` before exposing it. The opt-in never disables an existing key.
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
| Cannot sign in to WebUI | Configure an API key; sign in and restart unfinished OAuth after changing it. Behind an HTTPS reverse proxy, trust the public origin via `admin_allowed_origins` (see above) |
| Local 401 | Client key differs from the gateway key |
| Upstream 401 / 403 | Credential-level authentication circuit opens; inspect and log in again in the WebUI |
| 429 | Cool down that upstream model on the credential; later requests rebind automatically. All candidates cooling down still returns 429; with `--failover-max` the in-flight request is replayed on another credential instead |
| Upstream `service info not found` (11102) | Confirmed 400/404 model rejection backs off by `(backend, model)` domestically and `(account, product, model)` internationally. Return 404 when no candidate remains; half-open after 6 h, up to 24 h on repeats, cleared on success. Old international endpoint-wide entries no longer block accounts; inspect via `GET /admin/model-blocks` |
| Connection setup failure | Retry once on a fresh connection: `ConnectError` and `ConnectTimeout` fail before the first body byte, so the upstream holds nothing and replaying cannot double-bill |
| Post-send disconnect, read timeout or protocol error | No network replay, avoiding duplicate billing; logs include exception type and elapsed time |
| Client hangs up before a non-streaming response is ready | The upstream call is cancelled and its concurrency slot returned at once; the request is audited as `cancelled`, never as a completed answer. Streaming already behaves this way |
| Streaming request fails before the first byte | Reported with the real HTTP status, exactly like `stream=false`. A 200 carrying only an in-band `error` event is read by clients as an empty answer, so the session ends silently while the audit log records a success |
| Credential failover (`--failover-max`) | Off by default. When enabled, a failure before any byte reached the client is retried on another credential up to N times and audited as `success` with a `failover_recovered` marker. Qualifying failures: upstream HTTP 401/403/429/502/503/504 rejections and bodies the upstream provably never received (`ConnectError`/`ConnectTimeout`). Content-filter rejections, 502s from an already-open stream, read timeouts and protocol errors are never replayed; without another credential the original status surfaces. Billing note: 401/403/429/503 and transport failures happen at admission and cannot be billed; a 502/504 may already have been billed upstream, but its result never reached the client, so refusing to replay recovers no credit — it only turns a paid-for attempt into a broken session. Such replays are tagged `上游可能已处理该请求` in the log for reconciliation |
| Write-timeout replay (`--retry-write-timeout`) | Off by default. A write timeout proves the body was not fully sent, not that the upstream ignored the bytes it received, so it stays excluded from connect retry and failover until enabled. Long cross-border sessions fail here more often than in the handshake; enable only when the upstream is confirmed not to bill partial bodies. These replays carry the same `上游可能已处理该请求` log tag |
| Malformed tool calls | Compatible aggregate validation permits up to `--tool-call-max-retry` (default 3) additional generations, each consuming credits and recorded with its usage in the attempt details; exhaustion returns an error. Realtime mode never regenerates: it reports a protocol error before a success terminal |
| Empty or truncated upstream stream | No valid output, a missing end marker or an error is not reported as success |
| Content-filter rejection | With desensitization and `--no-compact`, a complete non-streaming filter-only rejection may receive one shorter-template retry on the same account. No streaming filter retry, circuit opening or account rotation |
| Slow responses | Inspect timing and failed attempts in the WebUI, then choose a faster model supported by the account |
| Same account invalidated elsewhere | Independent desktop/gateway refreshes may invalidate each other; prefer separate browser login or stop using the other client |

## Downgrades and rollback

Feature switches hold no hidden state: disabling a guard or mode stops it for new requests, and reverting source restores previous behavior. The exceptions are persisted settings and automation state: `control.sqlite3` stores WebUI settings, model rules and reward reservations, and older code rejects unknown fields. Before downgrading source, remove newly added startup options, stop the service, back up the **current** data directory, and use the narrowly scoped offline `settings.<name>` removal procedure above with a revision increment and integrity check. Do not restore a pre-upgrade database or mix WAL/SHM generations: doing so could roll back newer claims, sessions, revocations and account state. Rollback never undoes completed upstream check-ins, claims or travel dispatches.
