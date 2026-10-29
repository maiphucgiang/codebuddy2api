# Deployment

[Home](../README.md) · [简体中文](deployment.zh-CN.md)

Prefer the [WebUI](webui.md) for everyday account, model and log management. Run the commands below from the repository root.

## Configuration and data

On first setup, run `cp .env.example .env` and set `CODEBUDDY2API_KEY` to your own random key. It is the external admin inference key and the initial WebUI password until changed. Keep an existing `.env` and add only the settings you need.

| Setting | Purpose |
|---------|---------|
| `CODEBUDDY2API_IMAGE` | Compose image; defaults to the published `latest` tag |
| `CODEBUDDY2API_BIND` / `CODEBUDDY2API_PORT` | Native listener and Compose host mapping; default `127.0.0.1:8787` |
| `CODEBUDDY2API_AUTH_PATH` | Compose host data directory; defaults to `./auth`, mounted at `/data/auth` |
| `TZ` | Container time zone; daily guards follow it, and an unset value keeps UTC |
| `CODEBUDDY2API_ENV_FILE` | Optional Compose override for the env file path; defaults to `.env` |
| `CODEBUDDY2API_ALLOW_OPEN_NOAUTH` | Image/Compose compatibility opt-in, fixed `true` there. Only with no external or database keys does it anonymously expose inference **and account-pool billing**, with or without a placeholder token; any key row disables it |
| `CODEBUDDY2API_TRUSTED_PROXIES` | Trusted proxy IPs/CIDRs (comma separated); when the direct peer matches, the audit log records the client from `X-Forwarded-For`/`X-Real-IP` instead of the proxy address |
| `CODEBUDDY_AUTH_DIR` | Local Python data directory; defaults to the repository's `auth/`. Compose sets it to `/data/auth` inside the container |
| `CODEBUDDY_IMPORT_DIR` | Optional import directory; defaults to `imports/` under the data directory. Use container paths with Compose |
`.env.example` lists every active runtime variable with its default. Compose loads the whole file, so a key you comment out stays editable in the WebUI (tool metadata, Responses projection, origin allowlist, failover and similar). Zero disables aggregate/concurrency limits, per-item Responses trimming or extra retries, not the required positive inbound limit.

Compose loads `.env` into the container; shell variables still win for the image, host mapping and data path. The default host mapping is loopback. Set a random key, HTTPS and access restrictions before allowing remote connections. Container binding remains `0.0.0.0:8787`; change host exposure using `BIND/PORT`, not container listener arguments.

Mount the entire data directory on writable local storage, not just one SQLite file, and do not share it between instances. Stop the gateway and back up the whole directory before upgrading; see [data and backups](webui.md#data-and-backups).

## Docker Compose

### Use published images

Compose loads every key in `.env` into the container, so optional settings reach the gateway without repeating variable names; a missing `.env` still starts with the gateway defaults and the settings saved in SQLite. Set `CODEBUDDY2API_IMAGE` in `.env` to `ghcr.io/maiphucgiang/codebuddy2api:<version>` to pin an existing release, then run:

```bash
docker compose pull
docker compose up -d --no-build
```

Images support `linux/amd64` and `linux/arm64`. Version tags pin releases, `latest` follows stable releases and `edge` follows main. Features depend on the selected version; older images may not include the current source's WebUI.

### Build current source

```bash
docker compose build
docker compose up -d
```

The build includes the WebUI; Node.js and Python are not required on the host. Open `http://127.0.0.1:8787/dashboard` to add accounts. With no external key, get the initial `admin` password from `docker compose logs codebuddy2api`, then change it in **Settings → Admin account** and create inference keys in **Keys**.

### Upgrades and recreates

The WebUI **Updates** page can also update or roll back a running container in one click; see [Updates](webui.md#updates). Those changes live in the container's writable layer, so recreating the container returns to the image version. To follow new images instead, run `docker compose pull -q && docker compose up -d --no-build` from a host crontab.

After editing `.env`, repeat the appropriate `docker compose up -d` command to recreate containers whose configuration changed. Rebuild after updating local source; select and pull the new version when using published images. `docker compose restart` alone does not apply changed environment values. Preserve the data directory to retain login state.

## Reverse proxy and HTTPS

The default loopback mapping is the only safe exposure without extra work. When you put the gateway behind a reverse proxy on a domain:

- Terminate TLS at the proxy and forward the original Host and scheme (`proxy_set_header Host $host` and `X-Forwarded-Proto $scheme` in nginx).
- The WebUI compares the browser's `Origin` with the address the gateway actually sees. If the proxy rewrites the forwarded Host/scheme — for example HTTPS on the domain while the container sees HTTP — sign-in fails the Origin check. Trust your public address in **Settings → Extra trusted management origins（管理页额外信任来源）** or with `CODEBUDDY2API_ADMIN_ORIGINS` / `--admin-allowed-origins` instead of disabling CSRF protection; see [Management Origin checks](advanced.md#management-origin--csrf-switch).
- Configure an external key or create a database key before non-loopback exposure. Anonymous mode also exposes account-pool billing; never use it for public deployments.
## Local Python setup

Requires Python 3.12+ and uv. Build the interface only when the distribution does not include it, using Node.js 24 and the vp CLI pinned by `vite-plus` in `web/package.json`.

```bash
uv sync --locked --no-build --python 3.12
(cd web && vp install --frozen-lockfile && vp build)
uv run --locked --no-build converter.py --host 127.0.0.1 --port 8787
```

`--locked` stops instead of re-resolving when `uv.lock` is stale, so a source launch never upgrades packages. `.env` in the working directory is read automatically, so `uv run` needs no `--env-file`. Distribution archives already contain the WebUI; skip the frontend build there.

Local startup does not require `.env`. With `CODEBUDDY2API_KEY` from CLI, environment or `.env`, sign in as `admin` with that value; it remains the external admin inference key until removed. Without an external key, startup prints `admin` and a random initial password to the controlling terminal, or to stdout when no TTY is available. In Docker, use `docker compose logs codebuddy2api`. No inference key is created automatically: sign in and create one in **Keys**.

A legacy saved default key is migrated once to the initial admin password and a database admin key; database keys never become `CODEBUDDY2API_KEY`. Changing the account password decouples later logins from external-key changes and revokes existing sessions. Passwords are stored only as scrypt hashes.

Password output precedes the account commit. An output failure stops startup without writing the account; a commit failure retries initialization on the next start. Terminal or container-log retention remains outside the gateway's control, so change the initial password after first login.

Without uv, keep the same hash-locked set in a virtual environment:

```bash
python3 -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
python3 -m pip install --require-hashes --only-binary=:all: -r requirements.txt
python3 converter.py --host 127.0.0.1 --port 8787
```

Both startup paths read `.env` in the current working directory when present, never parent directories. Precedence: explicit CLI > process environment > `.env` > saved SQLite values > defaults. Database keys are never promoted to the external key; blank external values are treated as unset. Listener changes require restart; `CODEBUDDY2API_IMAGE/AUTH_PATH` only affect Compose.

### Configuration migration
### Admin password recovery

Use the interactive recovery command with the same data directory as the gateway:

```bash
uv run --locked --no-build converter.py admin reset-password
uv run --locked --no-build converter.py admin reset-password --username admin
```

It prompts twice on a TTY and accepts no password argument. It also works while the service is running; the next management request uses the new account epoch and revokes every existing session. In Docker, run `docker compose exec codebuddy2api python3 converter.py admin reset-password`.


`.env` values stay authoritative, so any runtime key listed there occupies its WebUI field. To move them into the configuration store, stop the service and run the offline command; it never edits `.env` itself:

```bash
python3 converter.py config migrate            # cwd/.env -> ./auth/control.sqlite3
python3 converter.py config migrate --env-file /srv/app/.env --data-dir /srv/app/auth
python3 converter.py config status --db /srv/app/auth/control.sqlite3   # read-only
```

Both commands resolve the env file first, then the data directory it declares (`CODEBUDDY_AUTH_DIR`), so `--data-dir`/`--db` only override the resolved target. Before writing, `migrate` verifies the target really is this project's control store (SQLite header, `user_version=3`, `control`/`runtime_state`/`gateway_secrets`/`api_keys`, readable payload, `quick_check`) and refuses while a running instance holds the lease or the write lock; `--force` bypasses the lease warning only. Deployment keys (key, listener, data paths) are never migrated. Inside a container there is no `.env` file, so use the process environment:

A v2 control database must be opened once by the new service to complete its v3 upgrade before `config migrate` can read it.

```bash
docker compose stop
docker compose run --rm --no-deps -e CODEBUDDY_AUTH_DIR=/data/auth \
  codebuddy2api python3 converter.py config migrate --no-env --data-dir /data/auth
```

A successful run writes the values and disables the Dashboard notice in one revision; delete the printed `.env` lines when you want the WebUI to supply them. Instances started before this release write no lease, so confirm the service is stopped once after upgrading. `config status` never writes. `--lang zh|en|auto` selects the CLI output language and, for `serve`, `login` and `config migrate`, also saves the interface language; see [configuration and CLI](advanced.md#configuration-and-cli).

> The audit schema is single-writer: stop the old instance before starting a new version, and never run two gateway versions against one auth directory.

### Upgrade and state migration

Release-archive installs and git checkouts can update or roll back in one click on the WebUI **Updates** page: it verifies the archive, backs up the control database, swaps the program files in place and restarts with the original arguments; see [Updates](webui.md#updates). For a manual upgrade, stop the gateway and back up the entire data directory first. Startup imports legacy JSON sessions, cooldowns, credit/trial history, catalogs and usage into `control.sqlite3` once. Old files remain as backups and are no longer read or updated. Invalid critical state or a failed migration stops startup; repair it or restore a backup rather than deleting ledgers to bypass validation.

SQLite state read/validation failures stop startup without deleting sessions or replacing state with empty defaults. Repair the control database before retrying; only rebuildable cache write failures may retain the current in-memory data with a warning.

All runtime logs use `logs.sqlite3`; legacy `--log` / `CODEBUDDY2API_LOG` settings warn and no longer write text files. Initial passwords go only to the terminal/startup output, and audit stores key IDs rather than key secrets. Restrict access to the data directory; use a private user directory on Windows.

Startup upgrades control schema v2 to v3 and migrates the legacy default key. Older versions cannot read the v3 control database; downgrade requires restoring a pre-upgrade backup while stopped, not merely old code reading stale JSON. Never overwrite new claims, dispatches or session revocations with an outdated backup.
## Dependency locks

`pyproject.toml` owns direct dependencies; `uv.lock` pins all resolved versions. The hash-locked `requirements.txt` is the generated pip input for Docker and CI:

```bash
uv lock
python3 scripts/export_requirements.py
```

Use `uv add`/`uv remove` for intentional dependency changes, then export and review the lock. Normal startup uses `--locked` and never upgrades packages. Metadata stays at the current `VERSION`; releases must update both version fields. Pip/Docker still require matching hashes and binary wheels; do not disable these checks.

Docker's build frontend, Node and Python images are pinned by multi-platform digest. When refreshing them, retain `linux/amd64` and `linux/arm64` support and verify the build. Locks prevent drift, not future vulnerabilities; security updates still require reviewed refreshes.

## CLI login

When the WebUI is unavailable, browser login also works without starting the server:

```bash
uv run --locked --no-build converter.py login
uv run --locked --no-build converter.py login --site intl --no-browser
uv run --locked --no-build converter.py login --site intl-codebuddy --no-browser
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
