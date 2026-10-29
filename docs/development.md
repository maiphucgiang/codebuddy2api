# Development preview

[Home](../README.md) · [简体中文](development.zh-CN.md)

The preview service runs the real gateway against a simulated CodeBuddy / WorkBuddy upstream. It needs no account and holds no real credentials, yet every page of the WebUI and every API answers, so you can work on the interface, debug clients and give demos.

## Start

```bash
uv run --locked --no-build python -B -m preview                   # http://127.0.0.1:8790, key: preview
uv run --locked --no-build python -B -m preview --port 8790 --data-dir /tmp/cb-preview
```

| Option | Default | Purpose |
|--------|---------|---------|
| `--host` / `--port` | `127.0.0.1` / `8790` | Listener |
| `--api-key` | `preview` | External admin inference key and initial WebUI password |
| `--data-dir` | new temporary directory | Keep the preview data across restarts; the directory must be empty or created by the preview |
| `--public` | off | Refuse actions unsafe on a shared demo; see [Public deployment](#public-deployment) |
| `--reset-hours` | `0` (never) | Exit after this many hours so a supervisor restarts with fresh data |
| other options | — | Passed to the gateway unchanged, such as `--admin-allowed-origins` |

Open `http://127.0.0.1:8790/dashboard` and sign in as `admin` with password `preview` (or your `--api-key` value). API clients use that value as the external admin key. The preview also seeds standard keys demonstrating expiry, IP and model restrictions. For hot reload while editing the frontend, keep the preview running and start the dev server against it:

```bash
cd web
CODEBUDDY_WEBUI_PROXY=http://127.0.0.1:8790 vp dev   # http://127.0.0.1:5174
```

## What is simulated

Everything below the gateway is simulated; everything in it is the real code. Requests, failover, cooldowns, billing, audit records and background automation run as they do in production.

| Area | Behaviour |
|------|-----------|
| Accounts | Eight synthetic accounts: domestic and international, CodeBuddy and WorkBuddy, an unlimited and a metered enterprise tenant, and one signed-out account whose refresh token was revoked |
| History | A month of request records, so the overview, logs and statistics open populated |
| Balances | Credit packages with different expiries; requests consume them, check-ins, trips, tasks and trial credits add to them |
| Models | Per-region catalogs with rates, capabilities and limits |
| Inference | Chat Completions, Responses and Anthropic Messages stream a simulated reply that quotes your message, with reasoning where the model supports it |
| Media | Seeded images and videos plus simulated generation; public WebUI media and model mutations are browser-local and reset on reload |
| Automation | Check-in, Buddy travel (a trip takes three minutes), growth tasks, trial credits and the international active check-in |
| Sign-in | Adding an account signs in a new synthetic account by itself after four seconds; the official login link it shows never needs to be opened |
| Search, fetch and MCP | `/v1/search`, `/v1/fetch` and the MCP `web_search` / `web_fetch` tools return simulated results on `example.com` |
| Updates | The release list holds only the current version, the mirror speed test downloads a simulated archive, and installing is disabled |

Upstream state lives in memory and starts over on every launch, while the gateway's own data stays in the data directory.

### Message directives

Put a directive anywhere in the last user message to steer the simulated model:

| Directive | Effect |
|-----------|--------|
| `[preview:tool]` / `[preview:tool=Name]` | Call the first or the named tool with placeholder arguments. Tools are never called without it, because a client may run whatever it is told to |
| `[preview:429]` | Answer 429 with a quota reset time, triggering cooldown and failover |
| `[preview:500]` | Answer an upstream server error |
| `[preview:slow]` | Stream slowly, to watch realtime streaming |
| `[preview:long]` | Append a long numbered list |

## Public deployment

`--public` makes the preview safe to share. Direct management writes for media deletion and task refresh, model rules and settings are refused alongside credential transfers, log clearing, archiving and updates. In the WebUI, media deletion and task refresh, media settings, model rules and model guards run in a per-tab in-memory overlay: the browser shows the completed operation, the server keeps its initial state, and a page reload restores that state. `--reset-hours` exits after the given time so the process manager starts over with fresh data. Options the preview does not know pass through to the gateway, such as the trusted management origin a reverse proxy needs.

The published `edge` image already contains the gateway and the WebUI, so mounting this directory is enough:

```yaml
services:
  preview:
    image: ghcr.io/maiphucgiang/codebuddy2api:edge
    restart: unless-stopped
    command: ["python3", "-B", "-m", "preview", "--public", "--reset-hours", "6", "--host", "0.0.0.0",
              "--port", "8790", "--admin-allowed-origins", "https://preview.example.com"]
    working_dir: /app
    volumes:
      - ./preview:/app/preview:ro
    ports:
      - "127.0.0.1:8790:8790"
    user: "65534:65534"
    read_only: true
    tmpfs:
      - /tmp:size=256m
```

Put a TLS reverse proxy in front of `127.0.0.1:8790` and list its public origin in `--admin-allowed-origins`, otherwise the WebUI sign-in fails the Origin check. The demo credentials `admin` / `preview` and the inference key `preview` are public on purpose; share them with the link.

## Isolation

- The preview never reads the repository's `.env` or `auth/`: it removes every `CODEBUDDY*` variable from its environment, works from its own data directory and refuses to use `auth/`.
- Every httpx request is answered by the simulation. Other outbound sockets and DNS lookups are refused, and the active check-in channel, which bypasses httpx, is replaced. Endpoints that are not simulated answer 501 and are logged once as `[preview] 未模拟的上游接口`.
- One-click updates are disabled, so the working tree is never replaced.
