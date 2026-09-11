# codebuddy2api

Use your **WorkBuddy / CodeBuddy (Tencent)** subscription as local **OpenAI- and Anthropic-compatible APIs**.

[中文文档](README.zh-CN.md)

## Features

- OpenAI Chat Completions / Responses and Anthropic Messages, with native tools / tool_calls and streaming SSE; automatic domestic / international and CLI / WorkBuddy backend routing through the original `/v1` endpoints
- **Seamless login**: add an account by scanning a QR code in your browser — the desktop client is **not** required
- Multi-account pool: per-session sticky routing, zero-multiplier (`x0.00`) model preference, least-expiring-credit first, automatic cooldown on 401/429
- Automatic token refresh and daily keepalive
- Optional credit balance via OpenAI billing endpoints (`/v1/dashboard/billing/*`)

## Quick start

### 1. Install

```bash
git clone https://github.com/maiphucgiang/codebuddy2api.git
cd codebuddy2api

uv venv
uv pip install -r requirements.txt
```

Or with plain venv: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`

### 2. Add an account (no desktop client needed)

```bash
uv run converter.py login
```

The command opens the login page in your browser. Scan the QR code, then wait for the terminal to confirm that your account has been saved, even if the browser already says login succeeded. Credentials are saved to `auth/` by default. You do not need to copy tokens or start the server first.

- International site (workbuddy.ai): `uv run converter.py login --site intl`.
- Server or no browser: `uv run converter.py login --no-browser`, then open the displayed link on another device to scan.
- Run the command again to add another account. Logging in to the same account updates its existing credential.
- Login links expire after 10 minutes. Press `Ctrl+C` to cancel.
- With a plain virtual environment, replace `uv run` with `python3`.

### 3. Start

Copy and edit the configuration before the first start; do not overwrite an existing `.env`. Compose-only image and port-mapping settings do not change the local Python listener.

```bash
cp .env.example .env
# Edit .env: API key, image policy, and other settings
uv run --env-file .env converter.py --desensitize --log converter.log
```

Listening on `http://127.0.0.1:8787` means it is up.

Plain `python3` does not load `.env`; set environment variables or CLI flags explicitly. With an API key enabled, include its Authorization header on API requests.

You can also add accounts from another terminal while the server runs; they are loaded on the next request by default. Use the same `CODEBUDDY_AUTH_DIR` for login and the server (default: `auth/`). A server started with `--auth-file` only uses the specified files.

If the desktop client is already logged in on this machine, its credential is imported automatically on first start. You can also place other accounts' `*.info` files in `auth/`.

### 4. Verify

```bash
curl http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/v1/models
# Lists available models across accounts; add Authorization if a key is set
```

## Client setup

Keep the OpenAI / Responses base URL at `http://127.0.0.1:8787/v1`. The backend automatically chooses an eligible account that supports the requested model, then uses that account's region and product. International-only models work at the same URL; no region prefix or new client parameter is needed.

### Codex CLI (recommended)

Codex CLI uses `/v1/responses`. Merge into `~/.codex/config.toml`:

```toml
[model_providers.workbuddy]
name = "WorkBuddy (via local converter)"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
env_key = "CODEBUDDY2API_KEY"

[profiles.workbuddy]
model = "glm-5.2"                   # or kimi-k2.7 / deepseek-v4-pro / auto
model_provider = "workbuddy"
```

```bash
export CODEBUDDY2API_KEY=any-value   # any value unless you started with --api-key
codex --profile workbuddy "your task"
```

### Claude Code / CC Switch

For Claude Code / Anthropic SDKs, use a base URL **without `/v1/messages`**: the SDK appends that path automatically.

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
export ANTHROPIC_AUTH_TOKEN=any-value  # use the configured API key when enabled
export ANTHROPIC_MODEL=deepseek-v4-pro
claude
```

In CC Switch, use `http://127.0.0.1:8787` for the Anthropic provider's Base URL, for both domestic and international accounts. Only clients asking for a complete endpoint should use `/v1/messages`; Anthropic SDKs append that path themselves.

Model names must be real Tencent-backend model names (no Anthropic→Tencent mapping). Keep `--desensitize` on for Claude Code.

### Other OpenAI-compatible clients

Cherry Studio / ZCode / LobeChat / NextChat / Open WebUI or your own SDK client:

- Base URL: `http://127.0.0.1:8787/v1`
- API Key: empty, or the `--api-key` you started with
- Model: `glm-5.2` / `deepseek-v4-pro` / `kimi-k2.7` / `auto` …

## Endpoints

| Endpoint | Description |
|------|------|
| `POST /v1/chat/completions` | OpenAI Chat Completions |
| `POST /v1/responses` | OpenAI Responses (Codex CLI) |
| `POST /v1/messages` | Anthropic Messages |
| `POST /v1/messages/count_tokens` | Anthropic token count estimate |
| `GET /v1/models` | Available models merged across accounts; adds `credits` multiplier and `credits_by_profile` breakdown |

These original paths serve all supported regions and products. `/cn` and `/intl` API prefixes are not registered and return 404. Across the three generation protocols, the upstream request always starts with a system message: an existing system message is moved to the front, or a default is inserted if absent; existing system messages and other content are retained.

| Shared endpoint | Description |
|------|------|
| `GET /health` | Public liveness only (`{"status":"ok"}`) |
| `GET /v1/dashboard/billing/subscription` | Total credit balance as `hard_limit_usd` |
| `GET /v1/dashboard/billing/usage` | Usage in cents, with daily cost breakdown |
| `GET/POST/DELETE /admin/credentials` | View / import / remove credentials |
| `POST /admin/oauth/start` · `GET /admin/oauth/poll` | Seamless login (see above) |
| `GET /admin/credits` · `POST /admin/checkin` | Credit balances / manual daily check-in |

Admin endpoints require `--api-key` when it is set. Use `/admin/credentials` for detailed pool status; `/health` never returns account, path or exception details.

### Credential imports

Place `.info` files in `auth/imports/`, or the server-side directory configured by `CODEBUDDY_IMPORT_DIR`. Only regular files directly inside that directory are accepted; symlinks, subdirectories and files over 1 MiB are rejected.

Send `POST /admin/credentials` with `{"path":"account.info"}` or the file's absolute path. The same filename updates an existing credential; the same UID under a different filename returns 409.

## Options

| Flag | Default | Description |
|------|------|------|
| `--host` | `127.0.0.1` | Listen address |
| `--port` | `8787` | Listen port |
| `--api-key` | — | Require this key from local clients |
| `--log` | — | Write request/response logs (50 MB rotation, 2 backups) |
| `--desensitize` | off | Compact runtime prompts and mask high-risk keywords (recommended for agent clients) |
| `--no-compact` | off | With `--desensitize`: keep fuller system prompts |
| `--auth-file` | scan `auth/` | Explicit credential file(s), repeatable |
| `--credit-price-cny` | `0.014` | CNY per credit for balance conversion |
| `--credit-price-usd` | `0.03` | USD per credit (international) |
| `--usd-rate` | `7.15` | CNY→USD rate for billing endpoints |
| `--model-catalog-ttl` | `21600` | Cloud model catalog cache TTL (seconds) |
| `--no-model-guard` | off | Disable local 404 for models outside the catalog |
| `--auto-trial [true/false]` | `false` | Enable one-time trial-credit claims for international WorkBuddy accounts |
| `--max-images` | `16` | Total images per request; `0` permits no images |
| `--image-policy` | `truncate` | `truncate` keeps the newest images; `error` rejects excess images with 413 |
| `--max-request-bytes` | `33554432` | Positive JSON byte limit after image processing and conversion (32 MiB) |
| `--log-body-limit` | `65536` | Body preview byte budget (64 KiB); `0` logs summaries only |

Named function choices are sent upstream as `required` with only that function available; invalid names are rejected locally. Set `stream: false` explicitly for JSON responses and `stream: true` for SSE.

Environment variables: `CODEBUDDY_AUTH_DIR` (credential dir), `CODEBUDDY_IMPORT_DIR` (allowed API import dir), `CODEBUDDY2API_KEY`, `CODEBUDDY2API_LOG`.

Limits also accept `CODEBUDDY2API_MAX_IMAGES`, `CODEBUDDY2API_IMAGE_POLICY`, `CODEBUDDY2API_MAX_REQUEST_BYTES`, and `CODEBUDDY2API_LOG_BODY_LIMIT`. CLI flags take precedence; restart after changing configuration.

Set `CODEBUDDY2API_AUTO_TRIAL=true` in `.env` to enable one-time trial-credit claims for `intl-work` accounts. It is off by default. Success or already-claimed results are persisted by account in `auth/trial-ledger.json`; failures wait at least 24 hours without immediate POST replay. Credits and eligibility are determined by the upstream; keep this state file when upgrading.

### Image and request limits

- Chat, Responses, and Anthropic requests count images across all history and tool results, including repeated images. Message/content array order determines recency, not nonstandard timestamps.
- The default keeps the newest 16 images without removing text or tool messages; image-only content receives a text placeholder when emptied. `--image-policy error` returns `413 / too_many_images` before contacting upstream.
- Requests still exceeding the byte budget after processing return `413 / request_too_large`, without further text truncation. Image count does not guarantee acceptable individual image sizes or model vision support. URL/base64 images can be converted; Responses `file_id` is unsupported.
- Logs contain bounded previews with image base64 and common credentials redacted, not complete original requests. Treat logs as private data.

## Docker

### Docker Compose

From the repository directory, copy the template to `.env` and edit it first. Keep an existing `.env` and add only the missing settings.

```bash
cp .env.example .env
# Edit .env: API key, bind address, port, credential directory, image policy, etc.
docker compose build
docker compose up -d
docker compose exec codebuddy2api python3 converter.py login --no-browser
```

The template builds the current source, binds only to `127.0.0.1:8787`, and keeps the newest 16 images per request. Before exposing another interface with `CODEBUDDY2API_BIND`, set your own random `CODEBUDDY2API_KEY` and restrict network access.

Compose automatically reads the declared variables from `.env`; shell environment variables take precedence. Ordinary interpolation retains compatibility with older Compose versions. Without `.env`, the Compose file still supplies compatibility defaults; new deployments should always copy the template.

Open the login link, scan, and wait for the terminal to confirm saving; add `--site intl` for international accounts. `CODEBUDDY2API_AUTH_PATH` defaults to `./auth` and mounts at `/data/auth`; added accounts load automatically. After editing `.env`, run `docker compose up -d` to recreate containers whose configuration changed, without logging in again.

### Published images

Set `CODEBUDDY2API_IMAGE` in `.env` to `ghcr.io/maiphucgiang/codebuddy2api:<version>`, then run:

```bash
docker compose pull
docker compose up -d --no-build
```

Published images support `linux/amd64` and `linux/arm64`. Version tags pin releases, `latest` follows stable releases, and `edge` follows `main`. Features depend on the selected image version; local changes require rebuilding.

### Docker CLI

Prepare `.env` first as above; these commands use local source and the default port and directory:

```bash
docker build -t codebuddy2api:local .
docker run -d --name codebuddy2api -p 127.0.0.1:8787:8787 \
  --env-file .env -v "$PWD/auth:/data/auth" \
  -e CODEBUDDY_AUTH_DIR=/data/auth codebuddy2api:local
docker exec -it codebuddy2api python3 converter.py login --no-browser
```

## Models

Use `/v1/models` as the source of truth: it merges the available sources across domestic and international accounts. Catalogs are cached in `auth/model-catalog.json` by account/tenant, region, product and client version, with a 6-hour TTL. New credentials trigger synchronization. Refresh failures retain only the same account's trusted cache; legacy unscoped root catalogs cannot authorize routing.

Beyond the standard OpenAI fields (`id` / `object` / `created` / `owned_by`), each model carries its multiplier: `credits` is the lowest multiplier across sources (`0.0` means zero-cost, `null` means no parseable multiplier was declared), and `credits_by_profile` breaks it down per product source, e.g. `{"intl-work": 0.0, "cn-cli": 0.03}`. Clients may ignore both extension fields.

Credential domain / token issuer determine the product profile; chat and token refresh use fixed origins with separately generated CLI / WorkBuddy identity headers:

| Profile | Chat / refresh origin |
|------|------|
| `cn-cli` | `https://copilot.tencent.com` |
| `cn-work` | `https://www.workbuddy.cn` |
| `intl-cli` | `https://www.codebuddy.ai` |
| `intl-work` | `https://www.workbuddy.ai` |

A concrete model can be scheduled across any region or product, but only among accounts whose own known catalog supports it. Each account retains its own catalog and balance; one account's capabilities or credits never authorize another. International credentials must have a known positive credit balance. Accounts whose own catalog declares the model as zero-multiplier (`credits: x0.00`, e.g. `deepseek-v4.1-flash` on international WorkBuddy) are preferred, then selection respects credit priority, cooldown and session stickiness. An account whose balance has reached zero leaves the paid-model rotation (it stops occupying the queue and stops failing on empty credit) while still serving the zero-multiplier models its own catalog declares; it rejoins automatically once the balance recovers. An ineligible or no-longer-preferred sticky account is rebound before sending, and the final account determines both the fixed host and identity headers. An upstream POST that has already been sent is not replayed against another account. Catalog / credential readiness failures return retryable 503; explicitly unsupported models return 404.

`auto` remains a scheduling alias for each eligible account's default, not permission to use every account or model. International accounts must declare `default-model` in their own catalog, and the upstream model is then `default-model`. Domestic WorkBuddy must declare `auto`; domestic CLI retains its legacy `auto` only with a known nonempty usable catalog. The alias observes the same balance, stickiness and cooldown constraints, including cooldown of the mapped upstream model.

## Troubleshooting

- **Local 401**: you started with `--api-key` but the client did not send the same key.
- **Upstream 401**: the account token died — re-add the account via seamless login.
- **429**: quota/rate limit — the gateway cools that model on that credential and routes to another; check `/admin/credentials` with your configured API key.
- **Network errors**: connection setup failures receive one delayed retry. Disconnects after sending, read/write timeouts, and HTTP errors are not replayed, to avoid duplicate billing. Logs include exception type and elapsed time.
- **Malformed tool calls**: failed aggregate validation permits up to three regenerations, then returns an error instead of broken calls. Regeneration may consume additional credits.
- **Empty upstream stream**: a stream containing only `stop` / `[DONE]` without content is treated as an error, not a successful empty answer.
- **Content-filter blocks**: usually triggered by agent runtime text; start with `--desensitize`, or `--desensitize --no-compact`.
- **Slow**: switch to a faster model, e.g. `deepseek-v4-flash`.
- **Same account used elsewhere**: a credential copied from a desktop client refreshes independently — with rolling refresh tokens they can kick each other off; prefer seamless-login accounts or stop using the account in the client.

## Disclaimer

For personal learning only — no commercial use. Not affiliated with Tencent, WorkBuddy, CodeBuddy, OpenAI, or Anthropic. This project only calls official APIs of accounts you are logged into; use it solely with subscriptions you legally own. You are solely responsible for your account, credentials, and all associated risks.

## License

[MIT](./LICENSE)


## Community

Thanks to the [LINUX DO](https://linux.do) community for providing an open and friendly platform for technical discussions.
