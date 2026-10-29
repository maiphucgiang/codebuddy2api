# AGENTS.md

Guidance for AI coding agents and contributors working in this repository.

## Project

codebuddy2api is a personal gateway that exposes a WorkBuddy / CodeBuddy (Tencent) subscription as OpenAI-compatible (Chat Completions, Responses) and Anthropic Messages APIs. It routes across multiple accounts on the domestic (`cn`) and international sites and ships a React WebUI at `/dashboard` for credentials, models, logs and settings. The backend is Python 3.12 + FastAPI; the frontend lives in `web/`.

## Commands

The backend is a uv project with `package = false`, so it runs from source without being installed. Install dependencies with `uv sync --locked`, or `pip install --require-hashes -r requirements.txt`.

```bash
uv run --locked --no-build converter.py             # serve on 127.0.0.1:8787 (subcommands: serve | login | config migrate|status | admin reset-password)
uv run --locked --no-build python -B -m preview     # real gateway + simulated upstream + synthetic accounts on :8790, key "preview"
uv run python -B tests/test_routing_scheduler.py    # one test file (each file is a standalone unittest script)
uv run python -B tests/test_version.py VersionTests.test_matching_tag   # one test case
for s in tests/test_*.py; do uv run python -B "$s"; done                # full suite, as CI runs it
uv run python -B -m pytest -q tests/                # pytest also works
python scripts/check_version.py                     # validate VERSION and the release tag
python scripts/export_requirements.py               # regenerate requirements.txt from uv.lock
```

Tests are offline: they mock the upstream and must never open real upstream connections. Every test file starts with `sys.path.insert(...)` so it can be run directly; keep that in new test files.

The preview (`preview/`, see `docs/development.md`) routes every httpx request into `preview/upstream.py`, a simulation of the upstream endpoints, and refuses all other outbound sockets. When a change makes the gateway call a new upstream endpoint, simulate it there too; an unsimulated endpoint answers 501 and logs `[preview] 未模拟的上游接口`.

The WebUI (`web/`) uses the pinned `vp` (vite-plus) toolchain with pnpm:

```bash
vp install --frozen-lockfile
vp check                 # lint + type-aware type check
vp test run              # vitest unit tests (src/**/*.test.tsx, jsdom)
vp build                 # emits web/dist, served by the gateway at /dashboard/
vp dev                   # dev server on :5174; CODEBUDDY_WEBUI_PROXY=http://127.0.0.1:8790 proxies /admin to the preview
vp exec playwright test                                       # browser e2e (web/e2e/*.spec.ts)
vp exec playwright test -c playwright.integration.config.ts   # against the real management API (tests/webui_fixture.py; WEBUI_PYTHON picks the interpreter)
```

## Releases and dependencies

- `VERSION` is the source of truth. `pyproject.toml` `version` must match it, and release tags must be exactly `v<VERSION>` (stable semver only). `tests/test_version.py` and CI enforce this.
- `requirements.txt` is generated from `uv.lock` and is hash-pinned; Docker and CI install it with `--require-hashes --only-binary=:all:`. Don't edit it by hand: change `pyproject.toml`, run `uv lock`, then run `scripts/export_requirements.py`.
- CI (`.github/workflows/docker.yml`) runs the backend tests, then the WebUI checks and Playwright. It builds the multi-arch GHCR image only for pushes to main (`edge`) and for version tags (the version tag plus `latest`). Changes that touch only documentation skip CI.

## Architecture

**`converter.py`** is the monolithic gateway entry point. It owns:
- The global `CONFIG` dict, which holds runtime state shared by everything: `control_store`, `audit_store`, `management`, ledgers and the resolved settings. Modules in `app/` receive it explicitly, or as `gateway.CONFIG` from the module object.
- `CredentialManager` (one `.info` credential plus token refresh) and `CredentialPool` (account selection, cooldowns, model and site blocks).
- The inference routes: `/v1/chat/completions`, `/v1/responses`, `/v1/messages`, `/v1/models`, `/v1/search`, `/v1/fetch` and `/v1/mcp`. `app/server_tools.py` adapts protocol server-side web tools (`web_search_*`, `web_fetch_*`, `web_search`, `web_search_options`) into gateway-executed tools and drives the extra upstream rounds.
- The legacy `/admin/*` credential routes.
- Stream failover (`_StreamFailure`, `_DeferredStreamResponse`), which holds back the response status until the upstream proves healthy.
- The background housekeeping and automation threads, started from `main()`.

Chat Completions is the internal canonical format. `app/adapters/` translates Responses and Anthropic requests to Chat and back:
- `responses_adapter` and `anthropic_adapter` handle the protocol translation.
- `responses_projection` trims the harness context.
- `chat_input` normalizes input messages.

**Importing the gateway has no side effects.** Importing `converter` only builds the bare FastAPI `app` with `InferenceResourcesMiddleware`. The management layer is wired up only when a server is started explicitly:
- `main()` calls `app/runtime_management.initialize()`, which opens `ControlStore` and `AuditStore`, resolves settings and builds `Management`.
- It then calls `install()`, which adds the WebUI management API (`admin_api.install_admin`), the middleware stack (inference auth, audit, policy scope, body and concurrency limits, request context) and the static WebUI pages.

Tests and `tests/webui_fixture.py` call the same functions to build isolated instances. Keep this boundary: new management features belong in `app/` and are installed there, never at import time.

**Persistence** lives in the data directory, set by `CODEBUDDY_AUTH_DIR` (default `auth/`; `/data/auth` in Docker):
- `*.info`: credential files, written atomically by `credential_io`.
- `control.sqlite3`: `ControlStore` keeps versioned settings and policy, and a stale revision raises `ConflictError` (HTTP 409). `state_store` keeps sessions, cooldowns and blocks, and imports legacy JSON snapshots once.
- `logs.sqlite3`: `AuditStore` keeps request and audit events. If the file can't be opened, the gateway falls back to `UnavailableAudit` and never recreates it.

**Settings** are declared once in `SCHEMA` in `app/settings.py`, which records each setting's type, environment variable, sensitivity and whether it applies immediately or needs a restart. Precedence is: explicit CLI flag > process environment > `.env` > value saved from the WebUI in SQLite > default. CLI and environment values lock the matching WebUI field. `.env.example` documents every runtime variable, so update it whenever you add a setting.

**Routing, catalogs and billing**:
- `site_routing` and `client_profiles` map each credential to its site, profile, endpoints and headers.
- `catalog_probe`, `catalog_index` and `model_catalog_view` merge the per-account model catalogs into public model IDs.
- `routing_scheduler` picks an account for each request.
- `model_rates`, `rate_measurement` and `credits` track billing.
- `benchmarks/bench_routing.py` is an offline benchmark for these hot paths.

**Account automation** applies to personal accounts only; enterprise accounts are excluded. The modules are `checkin`, `travel`, `buddy` and `buddy_task`, `growth_tasks`, `growth_chains` and `growth_events`, `trial_rewards` and `trial_management`, and `daily_chat`. They run as background threads or as `background_jobs`, whose progress shows in the WebUI.

**Prompt shaping** adapts client harness prompts to the fixed upstream CLI template when `--desensitize` is set, as it is in the Docker image. The modules involved are `desensitize`, `harness_context`, `content_filter`, `dsml` and `output_truncation`.

## Conventions

- Code comments and docstrings are in English. User-facing strings (HTTP error messages, logs, CLI help, `ValueError` text) are in Chinese. The WebUI goes through `web/src/i18n.tsx`, whose translations are type-checked.
- User docs come in pairs: `README.md` and every `docs/*.md` have a `*.zh-CN.md` counterpart, and both must be updated together.
- Never log or return tokens or account names. Pass anything that reaches logs or audit details through `safe_logging` or `audit_store.safe_label`.
- The data directory, `.env`, `*.info`, `*.sqlite3` and `converter.log*` hold secrets and are gitignored; never commit them. `docs/plans/` is also gitignored and holds local scratch notes only.
