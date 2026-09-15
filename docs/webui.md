# WebUI guide

[Home](../README.md) · [简体中文](webui.zh-CN.md)

## Open the console

Start the gateway using the [deployment guide](deployment.md), then open `http://127.0.0.1:8787/dashboard` and sign in with the current API key. Source installs need a frontend build; Docker builds include it. Use HTTPS unless connecting locally.

Management is locked without a key. After changing it, sign in again and restart any unfinished OAuth login.

## Common tasks

- **Overview:** select 1/7/30/90 days and automatic/hourly/daily granularity. Automatic uses hours for one day and days otherwise; ranges still follow UTC calendar days. Missing hourly history is marked, never reconstructed from daily totals, and detail cleanup preserves hourly aggregates. Official balances may include usage from other clients.
  - Hover or tap the trend for the period's requests, successes and failures; keyboard arrows and Home/End select points. Missing hourly spans stay empty.
- **Credentials:** select Mainland China (CN), International WorkBuddy or International CodeBuddy for browser login, or import `.info`/ZIP files. Distinguish manual disabling, credential-level authentication circuits and model-level 429 cooldowns. Disabling keeps files; deletion removes them and requires removing model bindings first. Exports preserve safe UTF-8 filenames (otherwise `credential.info`/`credentials.zip`) and contain plaintext credentials; do not share them.
  - Successful OAuth enrollment closes the drawer and refreshes the list. The official tab keeps `noopener` isolation and must be closed manually. Failures stay visible; closing the drawer stops polling.
  - Each row offers Token refresh, check-in and balance sync; batch check-in and sync stay separate. Sync updates balances, catalogs and usage without check-in, travel or trial claims. Busy maintenance returns 409; a client timeout does not cancel server work.
  - Automatic check-in and travel are persisted per account and apply live: on by default for domestic accounts, off internationally. International check-in can be enabled without a code update; inactive or unconfirmed activities never authorize claims. Disabled accounts run no automatic tasks.
  - Domestic check-in is followed by travel when its independent switch is on; already-checked-in accounts and accounts with automatic check-in off still check travel. “Travel status” only queries; “Claim / dispatch” claims arrivals, then rechecks idle state and the daily limit before randomly choosing location 1–4. Traveling accounts are not dispatched, and failed writes are not blindly retried.
  - Saving a preference does not claim immediately; it affects subsequent maintenance and cannot retract sent requests. The console shows last results, partial completion and uncertainty, retaining history on failure.
- **Models:** add independent mappings with public/upstream IDs and local enablement. Choose either specific accounts or a region with an optional product filter; switching modes clears the opposite binding. Unavailable candidates never cause out-of-scope fallback.
- **Logs:** filter requests and inspect failed attempts. Closing details or switching log type cancels pending detail loads. Clearing details keeps historical statistics.
- **Settings:** edit unlocked options; hot changes apply immediately, while restart-marked settings require a manual restart. Change locked options in the startup configuration; see [configuration precedence](advanced.md).
  - “Keep tool descriptions” is off by default and works across all three protocols, independently of prompt compaction; see [tool metadata retention](advanced.md#tool-metadata-retention) for configuration and limits.

Clearing **all logs and statistics** is irreversible. Enter the confirmation text shown in the dialog and re-enter the current API key. This does not delete credentials or gateway settings.

The sidebar remembers its icon-only mode. Drawers lock background scrolling, close on backdrop clicks or Esc, and restore focus; saves, imports and deletions prevent accidental dismissal while pending. Details use labeled fields and status groups with folded raw diagnostics. Glass surfaces fall back to solid colors when transparency is reduced or blur is unsupported.

The appearance icon offers light, dark and system-following modes. Four palettes affect light mode only; dark mode stays fixed and the light preference is retained. Preferences are browser-local. Drawers fade/slide in and out, retaining the scroll lock through exit; reduced-motion settings skip animation.

## Model mappings and statistics API

Multiple independent mappings may share an upstream model. Custom mapping IDs are management-only; clients use `public_id`. Existing account capability, balance and avoidance checks still apply. Legacy combined account/region scopes keep their original intersection until an explicit mode is selected during editing.

- Create: `POST /admin/models` with `public_id`, `upstream_id` and scope; edit: `PUT /admin/models/{id}`; delete: `DELETE /admin/models/{id}` (custom mappings only). Writes require the current `revision`.
- Preview: `POST /admin/models/preview` or `POST /admin/models/{id}/preview`. `credential_ids` is mutually exclusive with `region`/`profile`.
- Statistics: `GET /admin/dashboard?days=1&granularity=auto`, with `auto`, `hour` or `day`. Responses identify `range.granularity`, `range.partial` and UTC buckets without inventing missing hourly history.

## Data and backups

Data defaults to `auth/`, or `/data/auth` inside Docker. Local installs can set `CODEBUDDY_AUTH_DIR`; Compose uses `CODEBUDDY2API_AUTH_PATH` for the host directory.

| File | Contents |
|------|----------|
| `*.info` | Official plaintext credentials; never migrated into SQLite |
| `control.sqlite3` | Gateway settings, model rules and credential metadata |
| `logs.sqlite3` | Request details and independent aggregate statistics |

Auditing defaults to 30-day detail retention and a 256 MiB logical detail budget, **not a hard limit on database or directory disk usage**. Detail cleanup and eviction preserve aggregates. SQLite failure diagnostics have a separate budget, defaulting to 8192 bytes. Existing text logs are retained, not backfilled as precise statistics.

Mount the whole data directory on writable local storage, not just a single database file, and do not share it between gateway instances.

Stop the gateway before copying the entire directory, including databases, any WAL/SHM files, credentials and catalog/credit state files; do not back up only `.info` files. Keep this private data secure.

Back up control metadata before using new model rules or automation preferences. Reverting to older code requires the matching control-database snapshot, including its WAL/SHM state without mixing files; older readers reject the new fields. Rollback cannot undo completed upstream check-ins, claims or travel dispatches.

See [client configuration](clients.md) for API keys and URLs.
