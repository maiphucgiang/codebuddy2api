# Deployment

[Home](../README.md) · [简体中文](deployment.zh-CN.md)

Prefer the [WebUI](webui.md) for everyday account, model and log management. Run the commands below from the repository root.

## Configuration and data

On first setup, run `cp .env.example .env` and set `CODEBUDDY2API_KEY` to your own random key. Keep an existing `.env` and add only the settings you need.

| Setting | Purpose |
|---------|---------|
| `CODEBUDDY2API_IMAGE` | Compose image; the template uses `codebuddy2api:local` |
| `CODEBUDDY2API_BIND` / `CODEBUDDY2API_PORT` | Compose host binding / port; the template uses `127.0.0.1:8787` |
| `CODEBUDDY2API_AUTH_PATH` | Compose host data directory; defaults to `./auth`, mounted at `/data/auth` |
| `CODEBUDDY_AUTH_DIR` | Local Python data directory; defaults to the repository's `auth/`. Compose sets it to `/data/auth` inside the container |

Compose reads declared variables from `.env`; shell variables take precedence. Do not skip the template: without `.env`, compatibility defaults may expose all host interfaces. Set a random key, HTTPS and access restrictions before allowing remote connections.

Mount the entire data directory on writable local storage, not just one SQLite file, and do not share it between instances. Stop the gateway and back up the whole directory before upgrading; see [data and backups](webui.md).

## Docker Compose

### Build current source

```bash
docker compose build
docker compose up -d
```

The build includes the WebUI; Node.js and Python are not required on the host. Open `http://127.0.0.1:8787/dashboard` to add accounts.

### Use published images

Set `CODEBUDDY2API_IMAGE` in `.env` to `ghcr.io/maiphucgiang/codebuddy2api:<version>`, choosing an existing release, then run:

```bash
docker compose pull
docker compose up -d --no-build
```

Images support `linux/amd64` and `linux/arm64`. Version tags pin releases, `latest` follows stable releases and `edge` follows main. Features depend on the selected version; older images may not include the current source's WebUI.

After editing `.env`, repeat the appropriate `docker compose up -d` command to recreate containers whose configuration changed. Rebuild after updating local source; select and pull the new version when using published images. Preserve the data directory to retain login state.

## Local Python setup

Requires Python, uv, and Node.js with the vp CLI to build the interface:

```bash
uv sync --locked --no-build --python 3.12
(cd web && vp install --frozen-lockfile && vp build)
uv run --locked --no-build --env-file .env converter.py --desensitize
```

Configure `.env` as above before starting, then open `/dashboard` to add accounts. Rebuild the WebUI after changing frontend source.

Without uv, run `python3 -m venv .venv`, activate it, install dependencies with `pip install --require-hashes --only-binary=:all: -r requirements.txt`, and start with `python3 converter.py --desensitize`. **Plain Python does not load `.env`**; export environment variables or pass CLI flags explicitly.

Local Python binding uses `--host` and `--port`. Compose-only `CODEBUDDY2API_BIND`, `CODEBUDDY2API_PORT` and `CODEBUDDY2API_AUTH_PATH` do not change the local listener or data directory.

## Dependency locks

`pyproject.toml` owns direct dependencies; `uv.lock` pins all resolved versions. `requirements.in` and the hash-locked `requirements.txt` are generated compatibility files for pip, Docker and CI:

```bash
uv lock
python3 scripts/export_requirements.py
```

Use `uv add`/`uv remove` for intentional dependency changes, then export and review both locks. Normal startup uses `--locked` and never upgrades packages. Metadata stays at the current `VERSION`; releases must update both version fields. Pip/Docker still require matching hashes and binary wheels; do not disable these checks.

Docker's build frontend, Node and Python images are pinned by multi-platform digest. When refreshing them, retain `linux/amd64` and `linux/arm64` support and verify the build. Locks prevent drift, not future vulnerabilities; security updates still require reviewed refreshes.


## CLI login

When the WebUI is unavailable, browser login also works without starting the server:

```bash
uv run --locked --no-build --env-file .env converter.py login
uv run --locked --no-build --env-file .env converter.py login --site intl --no-browser
uv run --locked --no-build --env-file .env converter.py login --site intl-codebuddy --no-browser
```

The first command uses the domestic site. `--site intl` selects international WorkBuddy (`www.workbuddy.ai`); `--site intl-codebuddy` selects international CodeBuddy (`www.codebuddy.ai`). `--no-browser` prints a link you can open on another device. Even after the browser reports success, wait for the terminal to confirm that credentials were saved. Links expire after 10 minutes; press `Ctrl+C` to cancel.

In Docker, use `docker compose exec codebuddy2api python3 converter.py login --no-browser`; append `--site intl` or `--site intl-codebuddy` for the selected international product. The image must include the corresponding login option.

Login and the server must use the same `CODEBUDDY_AUTH_DIR`. Default directory scanning loads new accounts automatically; logging in again updates the same identity. A server started with `--auth-file` only uses the specified files.

With the default local mode and no `CODEBUDDY_AUTH_DIR`, startup imports missing credentials from an already logged-in desktop client. You can also add `.info` files to the managed directory. Independent desktop and gateway token refreshes may invalidate each other; prefer separate browser login.

## Without Compose

After preparing `.env`, build and run a local source image:

```bash
docker build -t codebuddy2api:local .
docker run -d --name codebuddy2api -p 127.0.0.1:8787:8787 \
  --env-file .env -v "$PWD/auth:/data/auth" \
  -e CODEBUDDY_AUTH_DIR=/data/auth codebuddy2api:local
```

This command explicitly uses the default port and directory, not Compose-specific port mappings. Add accounts in the WebUI or run `docker exec -it codebuddy2api python3 converter.py login --no-browser`.

## Verification

`curl http://127.0.0.1:8787/health` should return `{"status":"ok"}`. It checks liveness only, not account or model availability; inspect those in the WebUI. See [client configuration](clients.md) for API access and the [advanced reference](advanced.md) for errors and retry boundaries.
