# codebuddy2api

Use your **WorkBuddy / CodeBuddy (Tencent)** subscription as local **OpenAI- and Anthropic-compatible APIs**.

[中文文档](README.zh-CN.md)

## Features

- Serves `POST /v1/chat/completions`, `POST /v1/responses`, `POST /v1/messages`, `GET /v1/models` from your logged-in accounts, with native tools / tool_calls and streaming SSE
- **Seamless login**: add an account by scanning a QR code in your browser — the desktop client is **not** required
- Multi-account pool: per-session sticky routing, least-expiring-credit first, automatic cooldown on 401/429
- Automatic token refresh + daily keepalive, so credentials never die from expiry
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

### 2. Start

```bash
uv run converter.py --desensitize --log converter.log
```

Listening on `http://127.0.0.1:8787` means it is up.

### 3. Add an account (no desktop client needed)

```bash
# 1) request a login link
curl -X POST http://127.0.0.1:8787/admin/oauth/start
# → {"login_id": "oa_...", "verification_uri": "https://www.codebuddy.cn/login?...", "expires_in": 600}

# 2) open verification_uri in your browser and scan the QR code

# 3) poll until done — the credential is saved and hot-loaded into the pool
curl "http://127.0.0.1:8787/admin/oauth/poll?login_id=oa_..."
# → {"done": true, "uid": "...", "nickname": "...", "imported": ".../auth/<uid>.info"}
```

- Use `POST /admin/oauth/start?site=intl` for the international site (workbuddy.ai).
- If the WorkBuddy / CodeBuddy desktop client is already logged in on this machine, its credential is imported automatically on first start.
- You can also drop any `*.info` credential file into `auth/` — it is hot-loaded.
- All import channels validate the account uid and the issuer site; foreign or malformed files are rejected.

### 4. Verify

```bash
curl http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/v1/models
```

## Client setup

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

Claude Code uses `/v1/messages`. In CC Switch:

```json
{
  "DeepSeek-V4-Pro": {
    "base_url": "http://127.0.0.1:8787/v1/messages",
    "api_key": "",
    "model": "deepseek-v4-pro"
  }
}
```

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
| `POST /v1/messages` | Anthropic Messages (Claude Code / CC Switch) |
| `GET /v1/models` | Available models (cloud catalog synced, cached locally) |
| `GET /health` | Service + credential pool status |
| `GET /v1/dashboard/billing/subscription` | Total credit balance as `hard_limit_usd` |
| `GET /v1/dashboard/billing/usage` | Usage in cents, with daily cost breakdown |
| `GET/POST/DELETE /admin/credentials` | View / import / remove credentials |
| `POST /admin/oauth/start` · `GET /admin/oauth/poll` | Seamless login (see above) |
| `GET /admin/credits` · `POST /admin/checkin` | Credit balances / manual daily check-in |

Admin endpoints require `--api-key` when it is set.

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

Environment variables: `CODEBUDDY_AUTH_DIR` (credential dir), `CODEBUDDY2API_KEY`, `CODEBUDDY2API_LOG`.

## Docker

```bash
docker build -t codebuddy2api .
docker run -d --name codebuddy2api -p 8787:8787 \
  -v /path/to/auth:/data/auth \
  -e CODEBUDDY_AUTH_DIR=/data/auth \
  codebuddy2api
```

Any directory with `*.info` files works for the mount — add accounts afterwards via seamless login if you have none. `docker compose up -d --build` also works; edit the mount path in `docker-compose.yml` first.

## Models

Runtime `/v1/models` follows the cloud catalog (synced every 6 h, cached in `auth/model-catalog.json`); international models appear only while an international credential has credit. Fallback list when the cloud is unreachable:

`hy4-preview`、`hy4-preview-x`、`hy3`、`hy3-x`、`deepseek-v4-pro`、`deepseek-v4-flash`、`deepseek-v4.1-flash`、`deepseek-v3-2-volc`、`glm-5.3`、`glm-5.3-flash`、`glm-5.2`、`glm-5.1`、`glm-5.0`、`glm-5.0-turbo`、`glm-5v-turbo`、`glm-4.7`、`glm-4.6`、`glm-4.6v`、`minimax-m3`、`minimax-m2.7`、`minimax-m2.5`、`kimi-k3-1`、`kimi-k2.7`、`kimi-k2.6`、`kimi-k2.5`、`kimi-k2-thinking`、`hunyuan-chat`、`default`、`auto`

`auto` is a gateway-side scheduling alias. Actual availability depends on your account.

## Troubleshooting

- **Local 401**: you started with `--api-key` but the client did not send the same key.
- **Upstream 401**: the account token died — re-add the account via seamless login.
- **429**: quota/rate limit — the gateway cools that model on that credential and routes to another; check `/health`.
- **Content-filter blocks**: usually triggered by agent runtime text; start with `--desensitize`, or `--desensitize --no-compact`.
- **Slow**: switch to a faster model, e.g. `deepseek-v4-flash`.
- **Same account used elsewhere**: a credential copied from a desktop client refreshes independently — with rolling refresh tokens they can kick each other off; prefer seamless-login accounts or stop using the account in the client.

## Disclaimer

For personal learning only — no commercial use. Not affiliated with Tencent, WorkBuddy, CodeBuddy, OpenAI, or Anthropic. This project only calls official APIs of accounts you are logged into; use it solely with subscriptions you legally own. You are solely responsible for your account, credentials, and all associated risks.

## License

[MIT](./LICENSE)

<sub>Keywords: codebuddy to openai · codebuddy2api · workbuddy api proxy · workbuddy openai adapter · codex cli workbuddy · claude code workbuddy · tencent code assistant openai compatible api</sub>
