# WebUI guide

[简体中文](webui.zh-CN.md)

## Open the console

For a source install, configure the gateway as described in the [README](../README.md), then run from the repository root:

```bash
(cd web && vp install && vp build)
uv run --env-file .env converter.py --desensitize
```

Docker builds include the UI automatically. Open `http://127.0.0.1:8787/dashboard` and sign in with the current gateway API key. Set a key before using management features, and use HTTPS unless connecting locally.

After changing the key, sign in again and restart any unfinished OAuth login.

## Common tasks

- **Overview:** view requests, usage and credential health. Official account balances may include usage from other clients.
- **Credentials:** add accounts through OAuth or import `.info` files or ZIP archives. Disabling excludes an account from new requests without deleting its credentials. Exported files contain plaintext credentials; do not share them.
- **Models:** enable models, set public names or bind accounts. Explicit bindings never fall back to unselected accounts.
- **Logs:** filter requests and inspect failed attempts. Clearing details keeps historical statistics.
- **Settings:** change editable options here. Locked options must be changed in the startup configuration; restart-required options take effect after a manual restart.

Clearing **all logs and statistics** is irreversible. Enter the confirmation text shown in the dialog and re-enter the current API key. This does not delete credentials or gateway settings.

## Data and backup

Data is stored in `auth/` by default, or `/data/auth` in Docker. Use `CODEBUDDY_AUTH_DIR` to choose another directory.

Mount the entire data directory on writable local storage, and do not share it between gateway instances.

Stop the gateway before backing up the whole directory. It contains credentials, settings and logs; keep the backup private.
