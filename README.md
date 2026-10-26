# codebuddy2api

Use your **WorkBuddy / CodeBuddy (Tencent)** subscription as local **OpenAI- and Anthropic-compatible APIs**.

[中文文档](README.zh-CN.md)

- Chat Completions, Responses and Anthropic Messages, with tool calling and streaming.
- Compatible tool-schema and channel handling across all three inference protocols.
- Built-in **WebUI** for browser login, models, credentials, logs and settings — no desktop client required.
- Automatic multi-account routing across domestic and international sites, with credential refresh.
- Per-account automation: domestic personal accounts check in and dispatch Buddy travel by default, with independent switches; international check-in is opt-in. The growth center is personal-only: finished tasks are claimed automatically, with per-task switches for behavior chains; enterprise accounts are excluded.

**Live preview:** [codebuddy2api.elaina-love.com](https://codebuddy2api.elaina-love.com/dashboard), key `preview`. It runs the WebUI and APIs against a simulated upstream with synthetic accounts, holds no real credentials and resets every 6 hours; credential import and export, settings changes and log clearing are disabled there. See [Development preview](docs/development.md) to run it locally.

## Quick start

Requires Git and Docker Compose. The prebuilt GHCR image includes the WebUI; no local build is needed.

```bash
git clone https://github.com/maiphucgiang/codebuddy2api.git
cd codebuddy2api
cp .env.example .env
```

Edit `.env`: set `CODEBUDDY2API_KEY` to your own random key. Compose loads the whole file into the container, so every key applies and unknown keys are harmless. Preserve an existing `.env` on upgrades.

```bash
docker compose pull
docker compose up -d --no-build
```

Compose pulls `latest` by default (`CODEBUDDY2API_IMAGE=ghcr.io/maiphucgiang/codebuddy2api:latest`); images also publish the release version (for example `1.3.6`) and `edge` for `main`. Pin a version tag for reproducible deployments; image features belong to that version, not to unmerged source branches.

First use:

1. Open **http://127.0.0.1:8787/dashboard** and sign in with your API key.
2. In **Credentials**, add a domestic or international account through browser login, import an `.info` file, or paste refresh tokens for a chosen endpoint.
3. In **Models**, find an available model and use its public ID in your client.

The template binds to localhost only. Configure HTTPS and restrict network access before allowing remote connections; keep and securely back up the `auth/` data directory. To run current source or log in from a terminal instead, see the [deployment guide](docs/deployment.md).

## Client setup

| Protocol | Base URL |
|----------|----------|
| OpenAI Chat / Responses | `http://127.0.0.1:8787/v1` |
| Anthropic Messages | `http://127.0.0.1:8787` |
| MCP (web_search + web_fetch) | `http://127.0.0.1:8787/v1/mcp` |
| REST web search / fetch | `POST http://127.0.0.1:8787/v1/search` / `POST http://127.0.0.1:8787/v1/fetch` |
| Image & video generation | `POST http://127.0.0.1:8787/v1/images/generations` / `POST /v1/images/edits` / `POST /v1/videos` |

- Media generation is billed per upstream credit and only routes to verified account products; see [Media generation](docs/clients.md#media-generation).

- **API key:** the same key used to sign in to the WebUI.
- **Model:** a public ID from the WebUI or `GET /v1/models`.
- All accounts share these URLs; no region parameter is needed. Anthropic SDKs append `/v1/messages` themselves, so leave it out of the Base URL.
- The search and fetch endpoints never call a model. Declaring Anthropic `web_search_*` / `web_fetch_*` or OpenAI `web_search` server tools makes the gateway execute them itself. See [Web search and fetch](docs/clients.md#web-search-and-fetch).
- Tool parameters: across all three inference protocols, an empty parameter root becomes an object schema. Legacy field maps with boolean `required` flags are wrapped in `properties`, with required names collected at the root. A schema keyword such as `pattern` still counts as a field name when it holds a field. Standard schemas, roots with `$`/`x-` extensions, and nested schemas are left untouched.

[Codex CLI, Claude Code / CC Switch and other client examples →](docs/clients.md)

## Project structure

```
.
├── converter.py         # Gateway entry: protocol adaptation, routing, scheduling
├── app/                 # Management API, credentials, catalogs, audit and policy modules
├── web/                 # WebUI frontend (React/TypeScript; build output served by the gateway)
├── tests/               # Offline regression suites
├── preview/             # Development preview: real gateway, simulated upstream, synthetic accounts
├── docs/                # User guides, English and Chinese
├── examples/            # Client configuration examples
├── scripts/             # Version and dependency-lock tooling
├── Dockerfile           # Multi-stage image: frontend build + Python runtime
├── docker-compose.yml   # Recommended deployment
├── .env.example         # Every runtime environment variable, annotated
└── .github/workflows/   # CI: tests, image publishing, CodeQL
```

## Documentation

| Guide | Contents |
|-------|----------|
| [WebUI guide](docs/webui.md) | Accounts, model routing, audit logs, settings and backups |
| [Deployment](docs/deployment.md) | Published images, upgrades, reverse proxy/HTTPS, local setup and CLI login |
| [Client configuration](docs/clients.md) | Codex CLI, Claude Code, CC Switch and generic clients |
| [Advanced reference](docs/advanced.md) | Options, APIs, model scheduling, request limits and troubleshooting |
| [Development preview](docs/development.md) | Run the WebUI and APIs against a simulated upstream, without real accounts |

## FAQ

- **WebUI sign-in fails behind an HTTPS domain/reverse proxy?** Trust your public origin via `admin_allowed_origins` — see [Management Origin checks](docs/advanced.md#management-origin--csrf-switch).
- **No Docker?** After installing dependencies and the WebUI, run `uv run --locked --no-build converter.py` or `python3 converter.py` without `.env`. First local startup saves a default key and displays it once — see [Local Python setup](docs/deployment.md#local-python-setup).
- **Where is my data?** Everything lives in `auth/` (or `/data/auth` in Docker): credentials, settings and log databases — see [Data and backups](docs/webui.md#data-and-backups).
- **Which image tag should I use?** `latest` follows stable releases, `edge` follows main, version tags pin one release — see [Published images](docs/deployment.md#use-published-images).
- **Responses tool output/arguments compressed or need verbatim passthrough?** Set `responses_projection_mode` (default `balanced`, or `passthrough` to disable projection) and `responses_projection_max_bytes` (default `40000`, `0` disables per-item trimming). Balanced trimming keeps the head/tail and reports original bytes, estimated tokens and total lines; client addresses stay unchanged — see [Responses projection](docs/clients.md#responses-projection) and [details](docs/advanced.md#responses-projection).

## Disclaimer

For personal learning only — no commercial use. Not affiliated with Tencent, WorkBuddy, CodeBuddy, OpenAI, or Anthropic. This project only calls official APIs of accounts you are logged into; use it solely with subscriptions you legally own. You are solely responsible for your account, credentials, and all associated risks.

## License

[MIT](LICENSE)

## Community

Thanks to the [LINUX DO](https://linux.do) community for providing an open and friendly platform for technical discussions.
