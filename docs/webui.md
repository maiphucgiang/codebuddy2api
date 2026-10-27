# WebUI guide

[Home](../README.md) · [简体中文](webui.zh-CN.md)

## Open the console

Start the gateway using the [deployment guide](deployment.md), then open `http://127.0.0.1:8787/dashboard` and sign in with the current API key. Source installs need a frontend build; Docker builds include it. Use HTTPS unless connecting locally.

The first unconfigured local startup generates and saves a default key, shown once in the terminal and reused afterward. An explicitly empty key locks management. After changing the effective key, sign in again and restart unfinished OAuth logins.

## Overview

Select 1/7/30/90 days and automatic/hourly/daily granularity. Automatic uses hours for one day and days otherwise; ranges follow UTC calendar days. Missing hourly history is marked, never reconstructed from daily totals, and detail cleanup preserves hourly aggregates. Official balances may include usage from other clients.

Hover or tap the trend for the period's requests, successes and failures; keyboard arrows and Home/End select points. Missing hourly spans stay empty.

## Credentials

Add accounts by browser login — Mainland China (CN), International WorkBuddy or International CodeBuddy — import `.info`/ZIP files, or paste up to 50 refresh tokens for one chosen endpoint; each token is exchanged serially and the returned account block keeps enterprise tenants intact.

- **Runtime states:** distinguish manual disabling, credential-level authentication circuits and model-level 429 cooldowns. Disabling keeps files; deletion removes them and requires removing model bindings first. Exports preserve safe UTF-8 filenames (otherwise `credential.info`/`credentials.zip`) and contain plaintext credentials; do not share them.
- **Login drawer:** successful OAuth enrollment closes the drawer and refreshes the list. The official tab keeps `noopener` isolation and must be closed manually. Failures stay visible; closing the drawer stops polling.
- **Maintenance actions:** each row offers token refresh, check-in and balance sync; batch check-in and sync stay separate. Sync updates balances, catalogs and usage without check-in, travel or trial claims. Busy maintenance returns 409; a client timeout does not cancel server work.
- **Official credits:** a dedicated detail-drawer tab shows remaining credits, composition and per-segment expiry by package. Categories (plan, activity rewards, welcome benefits, enterprise quota, other) are inferred from package names. Sync failures, snapshots older than a day, partial pages and expired segments are flagged. Card and list expiry is the soonest credit expiry; token validity appears under sync status.

### Automation

Automatic check-in and travel are persisted per account and apply live: on by default for domestic personal accounts, off internationally. Enterprise accounts are outside the personal-only growth center, so they never travel and no longer render the upstream 403. Disabled accounts run no automatic tasks; inactive or unconfirmed activities never authorize claims. Saving a preference does not claim immediately and cannot retract sent requests; the console shows last results, partial completion and uncertainty.

- **Domestic travel** follows its own switch, including when check-in is already complete or disabled. "Travel status" only queries; "Claim / dispatch" claims arrivals, rechecks idle state and the daily limit, then chooses a current upstream location. Invalid location configuration stops dispatch.
- **Growth tasks:** eligible domestic personal accounts list live growth-center tasks in the credential drawer. Each task has an automation switch (off by default) and a one-click manual run; "run all" executes every enabled task plus the idempotent claim sweep, and task switches can be copied to selected accounts. The `auto_growth` master switch (on by default) enables the hourly accept/claim sweep and never starts chains on its own. Manual runs may spend credits through real conversations.
- **First-Buddy onboarding:** one agreement checkbox completes `first_buddy` through one real WorkBuddy conversation if needed, then adopts and dispatches. No separate task acceptance is required. The conversation requests at most 32 output tokens and may use credits; an eligible zero-rate model is preferred, and official completion is checked before adoption. `CODEBUDDY2API_AUTO_ACCEPT_BUDDY=true` preauthorizes enabled domestic accounts, including future imports, after restart; default is false, automatic follow-up respects the travel switch, and no other reward tasks, paid boxes or trial claims run.
- **International WorkBuddy trial credits** require manual confirmation in the credential row's drawer. "Refresh claim status" only reads the local ledger and never claims. On network or persistence failure, verify status before another attempt; environment-driven automatic claims are retired.

Reward claims and departures are durably reserved before sending. Stale or failed readbacks block repeat writes across restarts, without time-based expiry; only a reconciled state or definite pre-send cancellation/rejection releases them, and unconfirmed first-claim sends only reconcile through reads, even after 24 hours or a restart. Definitely unsent conversations may resume after settings or models recover; uncertain conversations are not repeated. Consent and outcomes are audited, and blocked automatic travel adds at most one warning per account per local day without failing check-in or balance sync. Keep `control.sqlite3` when upgrading.

## Models

Add independent mappings with public/upstream IDs and local enablement. Choose either specific accounts or a region with an optional product filter; switching modes clears the opposite binding. Unavailable candidates never cause out-of-scope fallback.

- **Model details** shows descriptions, capability states, token limits, reasoning options and safe raw metadata by product; differences remain separate. Declarations are read-only and do not guarantee native or measured support. Route previews use the draft binding scope.
- Media models share this catalog and routing rules; badges distinguish text-to-image, image editing and video models, with matching capability filters.
- International catalogs are shared and deduplicated. Inherited entries show "Shared catalog source" with expandable safe originals; native declarations and shared references remain distinguishable.
- The rate column shows `≈0.037x · 实测计费` for a model declared free that upstream charges, with the sample counts on hover, and `· 手填` for a manual rate. **Edit rule → 消费倍率** takes a manual rate per product and **一键探测倍率** sends one ≈8K-token request per product to tell real 0x from measured billing; **一键检测倍率** in the header does this for every model declared free. See [Measured rates and output limits](advanced.md#measured-rates-and-output-limits).
- The max output column overlays results from `scripts/probe_max_tokens.py`: red `32K ⚠ 钳制 12×` for silent clamping, green `48K ✓` / `64K ↑` when the claim holds or is exceeded, grey `≥40K` for a lower bound and `?` when nothing was measured.

## Logs

Filter requests and inspect failed attempts. Closing details or switching log type cancels pending detail loads. Clearing details keeps historical statistics.

Clearing **all logs and statistics** is irreversible. Enter the confirmation text shown in the dialog and re-enter the current API key. This does not delete credentials or gateway settings.

## Media

The **Media** page shows locally stored generation results: usage against the capacity limit, image and video totals, and active or failed video tasks.

- **Library** lists assets newest first with model, size and remaining retention; a card opens the preview drawer with metadata, download and a confirmation dialog. **Video tasks** refresh in-flight tasks on their bound account and link finished videos back to the library. Model catalogs and routing are managed under **Model routing**.
- Images are stored locally by default; without an asset base URL they return base64 without a public link. Explicit `response_format=b64_json` skips image persistence. Videos are always stored locally, and links appear once the base URL is set.
- **Media settings** (`media_public_base_url`, `media_max_total_mb`, `media_ttl_hours`) set the public link prefix, capacity (64–1048576 MB) and retention (1–8760 hours). Retention is derived from each asset's creation time and the current setting, so shrinking it expires existing assets immediately; the oldest are evicted on the next write or scheduled cleanup. Assets are also served publicly at `/v1/media/…` with immutable caching; see [Media generation](clients.md#media-generation).

## Settings

Edit unlocked options; hot changes apply immediately, while restart-marked settings require a manual restart. Change locked options in the startup configuration; see [configuration precedence](advanced.md#configuration-and-cli). The Dashboard banner lists runtime keys still taken from `.env`; stop the service, migrate them with `python3 converter.py config migrate`, or dismiss the banner once — after either action it never returns, even if `.env` gains more keys.

- "Keep tool descriptions" is off by default and works across all three protocols, independently of prompt compaction; see [tool metadata retention](advanced.md#tool-metadata-retention).
- "Model capability preflight" defaults to on. Disabling it affects new requests only, not metadata display, international image-run merging or existing security limits; see [model declarations](advanced.md#model-declarations-and-image-compatibility).
- "Extra trusted management origins" (`admin_allowed_origins`) fixes sign-in behind HTTPS reverse proxies; see [Management Origin checks](advanced.md#management-origin--csrf-switch).

## Updates

The **Updates** page shows the current version, deployment type and repository URL, and lists the stable GitHub Releases; pick a version (`/dashboard/updates/v<version>`) to read its notes or open it on GitHub. The gateway checks at startup and every 6 hours, and the sidebar flags a newer release; **Check for updates** refreshes on demand, returning the cached result when clicked again within 60 seconds. Set `HTTPS_PROXY` for the process if GitHub is unreachable.

Any release with an archive installs in one click: a newer version updates and an older one rolls back through the same steps. Release-archive installs, git checkouts and containers all qualify:

1. Download the chosen `codebuddy2api-<version>.tar.gz` and verify it against `SHA256SUMS` and the asset digest reported by the GitHub API.
2. Extract into `.update/` inside the install directory, checking layout and version and rejecting escaping paths. A rollback also confirms that the schema versions the target declares can read the current `control.sqlite3` and `logs.sqlite3`, and stops otherwise.
3. Back up the control database to `backups/` in the data directory (see [Data and backups](#data-and-backups)).
4. When `requirements.txt` changed, install dependencies with `uv pip` (or `pip` without uv) using `--require-hashes`.
5. Swap each top-level entry of the archive in with same-volume atomic renames, rolling every step back on failure; `.env`, the data directory, `.venv` and anything else outside the archive stay untouched.
6. Shut down gracefully (in-flight requests get up to 30 seconds), then restart in place with the original command line, environment and working directory, keeping the PID.

The page shows downloaded bytes, the current step and overall progress live; after the restart it polls until the new version answers, says so and reloads.

- **Git checkouts:** tracked files follow the release and uncommitted edits are overwritten, so commit or stash them first; untracked and ignored files inside replaced directories (such as `web/node_modules` or `docs/plans`) move into the new tree. HEAD does not move, so `git status` then shows the difference from the current commit.
- **Containers:** program files are written to the container's writable layer. Directories the image layer cannot rename are copied out and removed instead; later updates rename atomically again.
- **Download source:** the overview card shows the source in use and opens a drawer listing GitHub direct, the built-in mirrors (ghfast.top, gh-proxy.com, ghproxy.net and others) and one custom slot. Test every source at once or one at a time (each test downloads the first 512 KiB of the newest archive; a source returning anything but the archive counts as unavailable and cannot be chosen). A prefix typed into the custom slot is remembered once used, even after switching to another source. Mirrors only serve downloads: releases are still listed from the GitHub API, a mirrored archive must match the API digest, and a release without a digest cannot install through a mirror. `CODEBUDDY2API_UPDATE_MIRROR` sets and locks the source.
- **Version check:** a git checkout whose HEAD is not the commit of its version's tag, or that has uncommitted changes, shows a warning that the running code may not be that version (one-click updates leave HEAD in place and trigger it too). An edge image (built from main, recorded in its `build.json`) warns that installing a stable release in place may not match what edge already wrote; redeploy with `latest` or a version tag for the stable channel.
- **Check for updates** always answers with a notice: a new version, up to date, or why the check failed.
- Rolling back to a version without one-click updates means upgrading manually afterwards.

API: `GET /admin/update`, `POST /admin/update/check`, `POST /admin/update/install` (`{"tag": "v<version>"}`), `POST /admin/update/mirror` (`{"mirror": "<prefix or empty>", "custom": "<optional custom slot>"}`), `POST /admin/update/mirrors/test` (optional `{"mirror": "<prefix or empty>"}` tests one source).

## Interface behavior

The sidebar remembers its icon-only mode. Drawers lock background scrolling, close on backdrop clicks or Esc, and restore focus; saves, imports and deletions prevent accidental dismissal while pending. Details use labeled fields and status groups with folded raw diagnostics. Glass surfaces fall back to solid colors when transparency is reduced or blur is unsupported.

Action results appear as notices floating over the top of the page, or over an open drawer. Successes fade after a few seconds and results with unfinished accounts stay longer; hovering holds a notice, a click dismisses it, and refreshing the list or reloading the page clears them all. A finished job raises its notice only on the page that watched it run, so a reload does not repeat it. Progress banners for running jobs stay inline above the content.

The appearance icon offers light, dark and system-following modes. Four palettes affect light mode only; dark mode stays fixed and the light preference is retained. Language is stored in the configuration store, so the CLI and every browser share it; other preferences stay browser-local. Drawers fade/slide in and out, retaining the scroll lock through exit; reduced-motion settings skip animation.

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
| `control.sqlite3` | Settings, private default key, model rules, sessions, cooldowns, credit/reward state, usage and catalogs |
| `logs.sqlite3` | Request details and independent aggregate statistics |
| `media/` + `media.sqlite3` | Generated media files and their metadata; kept for the retention period and never included in configuration backups |

Auditing defaults to 30-day detail retention and a 256 MiB logical detail budget, **not a hard limit on database or directory disk usage**. Detail cleanup and eviction preserve aggregates. SQLite failure diagnostics have a separate budget, defaulting to 8192 bytes. Existing text logs are retained, not backfilled as precise statistics.

Mount the whole data directory on writable local storage, not just a single database file, and do not share it between gateway instances.

Stop the gateway before copying the entire directory, including databases, WAL/SHM files, credentials and migration backups; do not back up only `.info`. Protect the control database: it contains the default key and management sessions.

Configuration backups go to `backups/` in the data directory as 0600 tarballs. Each holds only an online `data/control.sqlite3` snapshot and `manifest.json`, which records the kind, the UTC time and the gateway version (plus the target version for a pre-update backup). Credentials (`*.info`), program files and `logs.sqlite3` are never included; the control database still holds the default key and management sessions, so keep backups private.

- **Pre-update:** every one-click update first writes `update-<old>-to-<new>-<UTC time>.tar.gz`.
- **Manual:** **Storage → Configuration backups → Back up now** writes `manual-<version>-<UTC time>.tar.gz`.
- **Automatic:** the **Automatic backups** drawer turns on scheduled `auto-<version>-<UTC time>.tar.gz` backups, sets the interval (`backup_interval_hours`, default 24) and the limit (`backup_keep`, default 7). Past the limit only the oldest automatic backups are deleted; manual and pre-update backups are never deleted automatically. `CODEBUDDY2API_BACKUP_AUTO`, `CODEBUDDY2API_BACKUP_INTERVAL_HOURS` and `CODEBUDDY2API_BACKUP_KEEP` lock these settings.

Each backup can be downloaded or deleted from the list. **Restore steps** explains the offline restore: install the version recorded in `manifest.json` from the **Updates** page if the program must go back too, then stop the gateway and copy `control.sqlite3` back; settings and state changed after the backup are lost. API: `POST /admin/storage/backups` creates a manual backup; `GET /admin/storage` lists backups with `backup_schedule`.

Legacy JSON is imported once; SQLite is authoritative afterward. Downgrades require a stopped gateway and matching state migration, never stale JSON overwriting new claims or revoked sessions. See [upgrade and state migration](deployment.md#upgrade-and-state-migration).

See [client configuration](clients.md) for API keys and URLs.
