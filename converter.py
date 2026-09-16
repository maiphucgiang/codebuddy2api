#!/usr/bin/env python3
"""Expose CodeBuddy and WorkBuddy through compatible Chat, Responses and Messages APIs."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import sys
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler as _default_http_exception_handler
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
import uvicorn

try:
    from app.desensitize import desensitize_body
except ImportError:  # Disable desensitization when its module is unavailable.
    def desensitize_body(body, roles=("system",), desensitize_harness_user=False,
                         desensitize_tools=False, compact_harness=False,
                         strip_tool_metadata=False):
        return body

from app.adapters.responses_adapter import (
    responses_request_to_chat,
    ResponsesStreamConverter,
)
from app.adapters.responses_projection import project_responses_chat_body
from app.adapters.anthropic_adapter import (
    anthropic_request_to_chat,
    AnthropicStreamConverter,
)

from app import auth_oauth
from app import trial_rewards
from app import buddy, checkin as checkin_service, model_policy, travel
from app.model_blocks import ModelBlocks
from app.client_hangup import ClientHungUp, await_or_hangup
from app.observability import (AuditMiddleware, observe_recovery, observe_route,
                               observe_usage, observe_attempt, observe_failure,
                               observe_failure_seq)
from app.credential_io import (CredentialFileError, read_import_file, atomic_write_credential,
                               credential_file_lock)
from app.upstream_io import (ChatSSEAccumulator, UpstreamHTTPError, UpstreamResponseError,
                             open_backend_stream, parse_retry_after, read_bounded_error)
from app.inference_resources import (AccountCapacity, InferenceResourcesMiddleware, inference_lifespan,
                                     request_resources, release_credential)
from app.request_context import SessionIdentifierError, current_context
from app.inference_auth import require_api_key
from app.content_filter import ContentFilterDetector, is_filter_error
from app.request_limits import ImageLimitError, apply_image_policy
from app.safe_logging import format_log_body, sanitize_log_text
from app.site_routing import (DOMESTIC, INTERNATIONAL, PROFILE_ENDPOINTS, site_for_auth, site_for_headers,
                              profile_for_auth, profile_for_headers, profile_region, profile_product,
                              profile_site, chat_url_for_headers, refresh_url_for_auth)
from app.client_profiles import CLI_VERSION, CLI_USER_AGENT, credential_headers, catalog_cache_key, account_key
try:
    from app import credits as credits_mod
except ImportError:  # Disable credit maintenance when its module is unavailable.
    credits_mod = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

APP_VERSION = Path(__file__).with_name("VERSION").read_text(encoding="utf-8").strip()
BACKEND = "https://copilot.tencent.com"
DEFAULT_DOMAIN = "www.codebuddy.cn"
CBC_VERSION = CLI_VERSION
USER_AGENT = CLI_USER_AGENT

# ---------------------------------------------------------------------------
# Platform-specific credential directories
# ---------------------------------------------------------------------------

def managed_auth_dir() -> Path:
    """Use CODEBUDDY_AUTH_DIR when set, otherwise the project's auth directory."""
    env_dir = os.environ.get("CODEBUDDY_AUTH_DIR")
    return Path(env_dir) if env_dir else Path(__file__).resolve().parent / "auth"


def auth_dirs() -> list[Path]:
    """Locate desktop credentials used only as seed files."""
    home = Path.home()
    plat = sys.platform
    if plat == "darwin":
        return [home / "Library" / "Application Support" / "CodeBuddyExtension" / "Data" / "Public" / "auth"]
    if plat == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        return [local / "CodeBuddyExtension" / "Data" / "Public" / "auth"]
    xdg = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
    return [xdg / "CodeBuddyExtension" / "Data" / "Public" / "auth"]


def seed_credentials():
    """Seed missing managed credentials without overwriting files; skip custom auth directories."""
    if os.environ.get("CODEBUDDY_AUTH_DIR"):
        return
    dst_dir = managed_auth_dir()
    dst_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(dst_dir, 0o700)
    except OSError:
        pass
    have_uids = {u for u in (_cred_identity(f) for f in dst_dir.glob("*.info")) if u}
    for src_dir in auth_dirs():
        if not src_dir.is_dir():
            continue
        for f in sorted(src_dir.glob("*.info")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:
                _log(f"[cred] 种子跳过（无法解析）: {f.name}: {e}")
                continue
            uid, verr = auth_oauth.validate_cred_data(data)
            if verr:
                _log(f"[cred] 种子跳过（入库校验失败：{verr}）: {f.name}")
                continue
            identity = _credential_identity(data)
            if uid and identity in have_uids:
                _log(f"[cred] 种子跳过（同账号已在自管目录）: {f.name}")
                continue
            dst = dst_dir / f.name
            if not dst.exists():
                try:
                    shutil.copyfile(f, dst)
                    os.chmod(dst, 0o600)
                    if uid:
                        have_uids.add(identity)
                    _log(f"[cred] 已复制桌面端凭据到自管目录: {f.name}")
                except OSError as e:
                    _log(f"[cred] 复制凭据失败 {f.name}: {e}")


def find_auth_files() -> list[Path]:
    """Find managed .info credential files."""
    d = managed_auth_dir()
    return sorted(d.glob("*.info")) if d.is_dir() else []


def _cred_uid(path) -> Optional[str]:
    """Read the account UID for deduplication, or return None when unavailable."""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        acct = d.get("account")
        if not isinstance(acct, dict):
            arr = d.get("accounts")
            acct = arr[0] if isinstance(arr, list) and arr and isinstance(arr[0], dict) else {}
        return acct.get("uid")
    except Exception:
        return None

def _credential_account(data: dict) -> dict:
    account = data.get("account")
    if not isinstance(account, dict):
        accounts = data.get("accounts") or []
        account = accounts[0] if isinstance(accounts, list) and accounts and isinstance(accounts[0], dict) else {}
    return account


def _credential_identity(data: dict) -> str:
    account = _credential_account(data)
    return account_key(profile_for_auth(data.get("auth") or {}), account.get("uid"), account.get("enterpriseId"))


def _cred_identity(path) -> str | None:
    try:
        return _credential_identity(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def find_auth_file() -> Path | None:
    files = find_auth_files()
    return files[0] if files else None


# ---------------------------------------------------------------------------
# Credential loading, refresh and persistence
# ---------------------------------------------------------------------------

class CredentialManager:
    """Load credentials and refresh expiring tokens with persistence."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._cached: dict | None = None
        self._mtime = None
        self._generation = 0

    def _read_raw(self) -> dict:
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _file_version(self):
        st = self.path.stat()
        return st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size

    def _load_if_stale(self):
        """Reload changed credentials and invalidate leases held by older requests."""
        mt = self._file_version()
        if self._cached is None or mt != self._mtime:
            self._cached = self._read_raw()
            self._mtime = mt
            self._generation += 1

    def _session(self) -> dict:
        self._load_if_stale()
        if self._cached is None:
            raise RuntimeError(f"无法读取 auth 文件：{self.path}")
        return self._cached

    def _is_expired(self) -> bool:
        s = self._session()
        expires_at = (s.get("auth") or {}).get("expiresAt") or 0
        # Treat tokens as expired 60 seconds early.
        return time.time() * 1000 >= (expires_at - 60_000)

    def _refresh_needed(self, margin_s, keepalive_s):
        summary = self.summary()
        now = time.time()
        exp = (summary.get("token_expires_at") or 0) / 1000
        last = (summary.get("last_refresh_time") or 0) / 1000
        return bool(summary.get("token_expired") or (exp and exp - now < margin_s)
                    or (keepalive_s > 0 and (last <= 0 or now - last >= keepalive_s)))

    def _refresh(self, margin_s=60, keepalive_s=0):
        with self._lock:
            if not self._refresh_needed(margin_s, keepalive_s):
                return False
            with credential_file_lock(self.path.parent, self.path.name):
                if not self._refresh_needed(margin_s, keepalive_s):
                    return False
                self._refresh_locked()
                return True

    def _refresh_locked(self):
        """Share the import lock so token refresh cannot overwrite a newer login."""
        s = self._session()
        auth = s.get("auth") or {}
        headers = self._build_headers_from(auth, _credential_account(s))
        headers["X-Refresh-Token"] = auth.get("refreshToken", "")
        headers["X-Auth-Refresh-Source"] = "plugin"
        url = refresh_url_for_auth(auth)
        try:
            with httpx.Client(timeout=15) as c:
                r = c.post(url, headers=headers, json={})
            data = r.json()
        except Exception as e:
            raise RuntimeError(f"刷新 token 网络失败：{e}")
        if data.get("code") != 0 or not data.get("data"):
            raise RuntimeError(f"刷新 token 失败：{data.get('msg', data)}")
        new_auth = dict(data["data"])
        if not isinstance(new_auth.get("accessToken"), str) or not new_auth["accessToken"]:
            raise RuntimeError("刷新接口未返回有效的 accessToken")
        new_auth["domain"] = new_auth.get("domain") or auth.get("domain")
        new_auth["refreshToken"] = new_auth.get("refreshToken") or auth.get("refreshToken")
        new_auth["lastRefreshTime"] = int(time.time() * 1000)
        if not new_auth.get("expiresAt") and new_auth.get("expiresIn"):
            new_auth["expiresAt"] = int(time.time() * 1000) + new_auth["expiresIn"] * 1000
        if not new_auth.get("refreshExpiresAt") and new_auth.get("refreshExpiresIn"):
            new_auth["refreshExpiresAt"] = int(time.time() * 1000) + new_auth["refreshExpiresIn"] * 1000
        updated = dict(s, auth=new_auth)
        atomic_write_credential(self.path.parent, self.path.name,
                                json.dumps(updated, ensure_ascii=False, indent=2).encode("utf-8"))
        self._cached = updated
        self._mtime = self._file_version()
        self._generation += 1

    def _build_headers_from(self, auth: dict, account: dict) -> dict:
        return credential_headers(auth, account)

    def get_headers(self) -> dict:
        """Return upstream headers with a current token, refreshing when necessary."""
        with self._lock:
            if self._is_expired():
                self._refresh()
            s = self._session()
            return self._build_headers_from(s.get("auth") or {}, _credential_account(s))

    def refresh_if_due(self, margin_s: int, keepalive_s: int) -> bool:
        """Refresh under the shared foreground/background lock after rechecking expiry."""
        return self._refresh(margin_s, keepalive_s)

    def invalidate(self):
        """Reload imported credentials while retaining the manager and refresh lock."""
        with self._lock:
            self._cached = None
            self._mtime = None
            self._generation += 1


    def summary(self) -> dict:
        with self._lock:
            s = self._session()
            auth = s.get("auth") or {}
            acct = _credential_account(s)
            profile = profile_for_auth(auth)
            return {
                "uid": str(acct.get("uid") or "") or None,
                "account_key": _credential_identity(s),
                "site": profile_site(profile),
                "profile": profile, "region": profile_region(profile), "product": profile_product(profile),
                "nickname": acct.get("nickname"),
                "enterpriseName": acct.get("enterpriseName"),
                "token_expires_at": auth.get("expiresAt", 0),
                "token_expired": self._is_expired(),
                "last_refresh_time": auth.get("lastRefreshTime") or 0,
            }


STICKY_TTL = 30 * 60        # Idle session binding lifetime in seconds
STICKY_MAX = 512            # Session binding capacity
CRED_COOLDOWN = 300         # Credential cooldown in seconds
MODEL_COOLDOWN = 600        # Model cooldown when a 429 omits reset time
MODEL_COOLDOWN_MAX = 86400  # Maximum model cooldown in seconds
MODEL_SITE_BLOCK_S = 6 * 3600      # Initial unsupported-model backoff
MODEL_SITE_BLOCK_MAX_S = 24 * 3600  # Maximum unsupported-model backoff
# An unsupported model must be routed to a different backend.
MODEL_NOT_SERVABLE_CODES = frozenset({"11102"})
_NOT_SERVABLE_MSG = re.compile(r"service info not found|model .{0,80}not (?:found|supported)", re.I)
CRED_REFRESH_MARGIN = 600   # Proactive refresh margin in seconds
CRED_KEEPALIVE_S = 24 * 3600   # Maximum idle interval before refreshing
CRED_KEEPALIVE_RETRY_S = 3600  # Keepalive retry interval, independent of expiry retries


def _msg_text(m: dict) -> str:
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(p.get("text", "") for p in c if isinstance(p, dict))
    return ""


def session_key(payload: dict) -> str | None:
    """Derive a stable session key from system instructions and the first user message."""
    msgs = payload.get("messages")
    if not msgs:
        inp = payload.get("input")  # Responses API
        if isinstance(inp, str):
            msgs = [{"role": "user", "content": inp}]
        elif isinstance(inp, list):
            msgs = inp
    msgs = msgs or []
    if not msgs:
        return None
    system = ""
    for m in msgs:
        if m.get("role") in ("system", "developer"):
            system += _msg_text(m)
        else:
            break
    first_user = next((_msg_text(m) for m in msgs if m.get("role") == "user"), "")
    if not system and not first_user:
        return None
    return hashlib.sha256((system + "\x00" + first_user).encode("utf-8", "replace")).hexdigest()[:32]


def _parse_reset_time(raw: bytes) -> float | None:
    """Parse a quota reset timestamp from a 429 response and return epoch seconds."""
    try:
        text = raw.decode("utf-8", "replace")
    except Exception:
        return None
    m = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\s*UTC\s*([+-]?\d+)", text)
    if not m:
        return None
    try:
        dt = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
        tz = timezone(timedelta(hours=int(m.group(3))))
        return dt.replace(tzinfo=tz).timestamp()
    except ValueError:
        return None




def _parse_not_servable(raw: bytes, status: int):
    """Recognize unsupported-model errors from code/message fields, excluding incidental IDs."""
    if status not in (400, 404) or not raw:
        return None
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, UnicodeError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    nodes = [payload]
    inner = payload.get("error")
    if isinstance(inner, dict):
        nodes.append(inner)
    code = msg = ""
    for node in nodes:
        for key in ("code", "errCode", "error_code"):
            value = node.get(key)
            if value is not None and str(value).strip():
                code = code or str(value).strip()
        for key in ("msg", "message"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                msg = msg or value.strip()
    if not code and not msg:
        return None
    if code in MODEL_NOT_SERVABLE_CODES or _NOT_SERVABLE_MSG.search(msg):
        return code or "11102", msg[:200]
    return None


def _block_model(model: str | None) -> str | None:
    """Track blocked models by public ID, normalizing the international auto alias."""
    return "auto" if model == "default-model" else model


def _dynamic_request_headers(skey: str | None) -> dict:
    """Generate upstream request IDs while keeping session IDs stable."""
    rid = secrets.token_hex(16)   # X-Request-ID == X-Conversation-Message-ID
    crid = secrets.token_hex(16)  # X-Conversation-Request-ID == X-Root-Request-ID == trace id
    span, parent = secrets.token_hex(8), secrets.token_hex(8)
    if skey:
        conv = str(uuid.UUID(hex=hashlib.sha256(skey.encode()).hexdigest()[:32]))
    else:
        conv = str(uuid.uuid4())
    return {
        "X-Conversation-ID": conv,
        "X-Request-ID": rid,
        "X-Conversation-Message-ID": rid,
        "X-Conversation-Request-ID": crid,
        "X-Root-Request-ID": crid,
        "X-Trace-ID": crid,
        "traceparent": f"00-{crid}-{span}-01",
        "b3": f"{crid}-{span}-1-{parent}",
        "X-B3-TraceId": crid,
        "X-B3-SpanId": span,
        "X-B3-ParentSpanId": parent,
        "X-B3-Sampled": "1",
    }


class CredentialPool:
    """Manage credential discovery, reloads, sticky sessions, cooldowns and refresh."""

    def __init__(self, paths: list[Path] | None = None, scan: bool = False,
                 blocks_path: Path | None = None):
        self._lock = threading.RLock()
        self._entries: list[dict] = []   # {id, cm, fail_until}
        self._sticky: "OrderedDict[str, tuple[str, float]]" = OrderedDict()
        self._model_fail: dict[tuple[str, str], float] = {}  # Per-credential/model 429 expiry
        # Keep unsupported-model backoff isolated by backend and model.
        self._blocks = ModelBlocks(blocks_path, ttl_s=MODEL_SITE_BLOCK_S, max_ttl_s=MODEL_SITE_BLOCK_MAX_S)
        self._rr = {None: 0, "cn": 0, "intl": 0}
        self._ledger = None              # Prefer credits expiring sooner.
        self._capacity = AccountCapacity()
        self._scan = scan                # Rescan credentials before selection.
        self._ignored_duplicates: set[str] = set()
        self._sync_pending: set[str] = set()
        self._syncing: set[str] = set()
        self._sync_event = threading.Event()
        self._sync_retry: dict[str, float] = {}
        self._sync_attempts: dict[str, int] = {}
        self.reload(paths or [])
        if self._scan:
            self._rescan()             # Discover credentials at startup.

    def reload(self, paths: list[Path], *, reset: bool = True):
        """Reset authentication only for changed or imported files and schedule catalog refresh."""
        with self._lock:
            by_id = {entry["id"]: entry for entry in self._entries}
            have_uids = {entry["account_key"]: entry["id"]
                         for entry in self._entries if entry.get("uid")}
            for path in paths:
                cid = str(Path(path).resolve())
                if not os.path.exists(cid):
                    continue
                entry = by_id.get(cid)
                if entry is not None:
                    if reset:
                        entry["cm"].invalidate()
                    try:
                        summary = entry["cm"].summary()
                    except Exception:
                        continue  # A damaged file must not block other credentials.
                    generation = entry["cm"]._generation
                    identity = summary["account_key"]
                    changed = reset or generation != entry.get("generation")
                    if changed:
                        old_identity = entry.get("account_key")
                        if old_identity != identity:
                            self._model_fail = {key: until for key, until in self._model_fail.items() if key[0] != cid}
                            self._sticky = OrderedDict((key, value) for key, value in self._sticky.items() if value[0] != cid)
                        if entry.get("uid"):
                            have_uids.pop(old_identity, None)
                        entry.update(uid=summary.get("uid"), profile=summary["profile"], site=summary["site"],
                                     account_key=identity, generation=generation, catalog_dirty=True)
                        self._bind_entry(entry)
                        if reset or old_identity != identity:
                            entry.update(fail_until=0.0, keepalive_after=0.0)
                        if entry.get("uid"):
                            have_uids[identity] = cid
                        self._queue_sync(cid)
                    continue
                manager = CredentialManager(Path(cid))
                try:
                    summary = manager.summary()
                except Exception:
                    summary = {}
                uid = summary.get("uid")
                profile = summary.get("profile", "cn-cli")
                identity_key = summary.get("account_key")
                if uid and identity_key in have_uids:
                    if cid not in self._ignored_duplicates:
                        _log(f"[cred] 忽略重复账号凭据: {Path(cid).name}（同产品账号与 {Path(have_uids[identity_key]).name} 重复）")
                        self._ignored_duplicates.add(cid)
                    continue
                entry = {"id": cid, "cm": manager, "fail_until": 0.0 if summary else time.time() + CRED_COOLDOWN, "uid": uid,
                         "site": summary.get("site"), "profile": profile, "generation": manager._generation,
                         "account_key": identity_key, "catalog_dirty": True}
                self._bind_entry(entry)
                self._entries.append(entry)
                by_id[cid] = entry
                self._ignored_duplicates.discard(cid)
                if uid:
                    have_uids[identity_key] = cid
                self._queue_sync(cid)

    def _queue_sync(self, cid):
        entry = next((entry for entry in self._entries if entry["id"] == cid), None)
        if entry is not None and not model_policy.credential_enabled(CONFIG, entry):
            return
        self._sync_pending.add(cid)
        self._sync_retry.pop(cid, None)
        self._sync_attempts.pop(cid, None)
        self._sync_event.set()
        if CONFIG.get("cred_pool") is self:
            _publish_model_cache()
        else:
            invalidate_model_table()

    def begin_sync(self, *, all_entries=False):
        """Drain refresh work and clear its wake event under the same lock."""
        with self._lock:
            active = {entry["id"] for entry in self._entries if model_policy.credential_enabled(CONFIG, entry)}
            self._sync_pending.intersection_update(active)
            self._sync_retry = {cid: deadline for cid, deadline in self._sync_retry.items() if cid in active}
            due = {cid for cid, deadline in self._sync_retry.items() if deadline <= time.monotonic()}
            self._sync_pending.update(due)
            ids = active if all_entries else set(self._sync_pending)
            self._sync_pending.difference_update(ids)
            if not self._sync_pending:
                self._sync_event.clear()
            self._syncing.update(ids)
            return ids

    def end_sync(self, ids, failed=()):
        with self._lock:
            self._syncing.difference_update(ids)
            present = {entry["id"] for entry in self._entries if model_policy.credential_enabled(CONFIG, entry)}
            for cid in ids:
                if cid in failed and cid in present and cid not in self._sync_pending:
                    attempt = min(self._sync_attempts.get(cid, 0) + 1, 5)
                    self._sync_attempts[cid] = attempt
                    self._sync_retry[cid] = time.monotonic() + min(60 * 2 ** (attempt - 1), 900)
                else:
                    self._sync_retry.pop(cid, None)
                    self._sync_attempts.pop(cid, None)

    def sync_pending(self, region=None):
        with self._lock:
            pending = self._sync_pending | self._syncing | self._sync_retry.keys()
            return bool(pending) if region is None else any(
                entry["id"] in pending and (profile := self._entry_profile(entry))
                and profile_region(profile) == region for entry in self._entries)

    def sync_wait(self, periodic_delay):
        with self._lock:
            retry_delay = min(self._sync_retry.values(), default=float("inf")) - time.monotonic()
        return max(0, min(periodic_delay, retry_delay))

    def apply_if_current(self, cm, generation, update):
        """Run updates only for enabled accounts with the current credential lease."""
        with self._lock, cm._lock:
            entry = next((entry for entry in self._entries if entry["cm"] is cm), None)
            if entry is None or not model_policy.credential_enabled(CONFIG, entry):
                return False
            if not self._lease_matches(cm, generation):
                self._queue_sync(entry["id"])
                return False
            self.reload([cm.path], reset=False)
            update()
            return True

    def prune(self):
        """Remove missing credential files and their session bindings."""
        with self._lock:
            self._ignored_duplicates = {p for p in self._ignored_duplicates if os.path.exists(p)}
            before = len(self._entries)
            removed = [e for e in self._entries if not os.path.exists(e["id"])]
            for entry in removed:
                if self._ledger is not None:
                    self._ledger.remove(entry["id"])
            self._entries = [e for e in self._entries if e not in removed]
            if len(self._entries) != before:
                ids = {e["id"] for e in self._entries}
                self._sync_pending.intersection_update(ids)
                self._syncing.intersection_update(ids)
                self._sync_retry = {cid: deadline for cid, deadline in self._sync_retry.items() if cid in ids}
                self._sync_attempts = {cid: count for cid, count in self._sync_attempts.items() if cid in ids}
                invalidate_model_table()
                self._sticky = OrderedDict((k, v) for k, v in self._sticky.items() if v[0] in ids)
                self._model_fail = {k: v for k, v in self._model_fail.items() if k[0] in ids}
                if CONFIG.get("cred_pool") is self:
                    _publish_model_cache()


    def find_by_uid(self, uid: str, identity: str | None = None) -> Optional[str]:
        """Find a credential ID by account UID for import conflict checks."""
        with self._lock:
            for e in self._entries:
                if e.get("uid") == uid and (identity is None or e.get("account_key") == identity):
                    return e["id"]
        return None

    def set_ledger(self, ledger):
        """Attach the credit ledger used for expiry-aware credential selection."""
        with self._lock:
            self._ledger = ledger
            self.reload([Path(entry["id"]) for entry in self._entries], reset=False)
            for entry in self._entries:
                self._bind_entry(entry)

    def _bind_entry(self, entry):
        if self._ledger is not None:
            if entry.get("account_key"):
                self._ledger.bind_identity(entry["id"], entry["account_key"])
            else:
                self._ledger.remove(entry["id"])

    def entries(self) -> list[dict]:
        """Return credential snapshots for account maintenance."""
        with self._lock:
            return [dict(e) for e in self._entries]

    def _expiry_rank(self, e: dict) -> tuple:
        """Order by earliest credit expiry, placing unknown balances last."""
        exp = self._ledger.soonest_expiry_of(e["id"]) if self._ledger else None
        return (exp is None, exp or 0.0)
    def _rescan(self):
        self.prune()
        paths = find_auth_files() if self._scan else [Path(entry["id"]) for entry in self.entries()]
        self.reload(paths, reset=False)

    def _healthy(self, e: dict) -> bool:
        return model_policy.credential_enabled(CONFIG, e) and time.time() >= e["fail_until"]

    @staticmethod
    def _entry_profile(entry):
        try:
            return entry["cm"].summary().get("profile", "cn-cli")
        except Exception:
            return None

    @classmethod
    def _entry_site(cls, entry):
        profile = cls._entry_profile(entry)
        return profile_site(profile) if profile else None

    def _zero_balance(self, entry, profile) -> bool:
        """Restrict a confirmed zero-balance account to its advertised zero-rate models."""
        balance = (self._ledger.entry(entry["id"]).get("credits") or {}) if self._ledger else {}
        if not balance:
            return False
        try:
            return (bool(balance.get("intl")) == (profile_region(profile) == "intl")
                    and float(balance.get("credits") or 0) <= 0)
        except (TypeError, ValueError):
            return False

    def _has_credit(self, entry, profile):
        balance = (self._ledger.entry(entry["id"]).get("credits") or {}) if self._ledger else {}
        if not balance:
            return profile_region(profile) == "cn"
        try:
            return (bool(balance.get("intl")) == (profile_region(profile) == "intl")
                    and float(balance.get("credits") or 0) > 0)
        except (TypeError, ValueError):
            return False

    def _eligible(self, entry, model, *, region=None, profile=None, rule=None):
        if not model_policy.route_allowed(CONFIG, entry, model, rule=rule):
            return False
        actual = self._entry_profile(entry)
        profile = profile or actual
        if not profile or profile != actual or not _in_region(profile, region):
            return False
        configured = {candidate for item in self._entries if (candidate := self._entry_profile(item))
                      and _in_region(candidate, region)}
        if profile not in _model_profiles(model, region, configured):
            return False
        if CONFIG.get("account_catalogs") is not None or CONFIG.get("model_cache") is not None:
            try:
                identity = entry["cm"].summary()["account_key"]
            except Exception:
                return False
            if identity != entry.get("account_key"):
                return False
            account = (CONFIG.get("account_catalogs") or {}).get(identity) or {}
            models = _account_scope(account, "serves")
            if account.get("profile") != profile or models is None:
                return False
            usable = _usable_models(models)
            supported = any(item["id"] == _upstream_model(model, profile) for item in usable)
            cli_auto = model == "auto" and profile == "cn-cli" and bool(usable)
            # Disabling the guard must not borrow another account's model capabilities.
            declared = any(item["id"] == _upstream_model(model, profile)
                           for item in _models_for_profile(profile, configured, scope="serves"))
            passthrough = (model != "auto" and not declared and not CONFIG.get("model_guard")
                           and len(configured) == 1)
            if model and not (supported or cli_auto or passthrough):
                return False
        # Zero-balance accounts may only use their own advertised zero-rate models.
        return (not model or self._has_credit(entry, profile)
                or self._model_free(entry, model, profile=profile))

    def _model_free(self, entry, model: str | None, *, profile=None) -> bool:
        """Check whether this account advertises the model as zero-rate."""
        if not model or model == "auto":
            return False
        profile = profile or self._entry_profile(entry)
        if not profile:
            return False
        accounts = CONFIG.get("account_catalogs")
        if accounts is not None or CONFIG.get("model_cache") is not None:
            account = (accounts or {}).get(entry.get("account_key")) or {}
            if account.get("profile") != profile:
                return False
            return _model_free(_account_scope(account, "serves"), model, profile)
        return _model_free(_models_for_profile(profile), model, profile)

    @classmethod
    def _entry_endpoint(cls, e: dict) -> str | None:
        """Return the credential's backend endpoint for isolated model availability checks."""
        profile = cls._entry_profile(e)
        return PROFILE_ENDPOINTS.get(profile) if profile else None

    def _model_servable(self, e: dict, model: str | None) -> bool:
        """Check backend/model backoff, skipping the check when no model is supplied."""
        if not model:
            return True
        endpoint = self._entry_endpoint(e)
        if not endpoint:
            return True
        return time.time() >= self._blocks.until(endpoint, _block_model(model))

    def _model_healthy(self, e: dict, model: str | None) -> bool:
        """Check this credential's model-specific 429 cooldown."""
        if not model:
            return True
        routed_model = _upstream_model(model, self._entry_profile(e))
        return time.time() >= self._model_fail.get((e["id"], routed_model), 0.0)

    def _evict_sticky(self):
        now = time.time()
        while self._sticky:
            k, (_, ts) = next(iter(self._sticky.items()))
            if now - ts > STICKY_TTL or len(self._sticky) > STICKY_MAX:
                self._sticky.pop(k)
            else:
                break

    def _candidates(self, model: str | None, *, region=None, tried=()) -> list[dict]:
        """Exclude tried credentials and rank candidates by zero rate and credit expiry."""
        tried = set(tried)
        healthy = [entry for entry in self._entries if entry["cm"] not in tried
                   and self._healthy(entry)
                   and self._eligible(entry, model, region=region) and self._model_healthy(entry, model)
                   and self._model_servable(entry, model)]
        if not healthy:
            return []
        # Prefer zero-rate models, then earlier credit expiry; unknown balances sort last.
        healthy.sort(key=lambda entry: (not self._model_free(entry, model), *self._expiry_rank(entry)))
        return healthy

    @staticmethod
    def _capacity_error():
        return HTTPException(status_code=503, headers={"Retry-After": "3"}, detail={"error": {
            "message": "符合当前路由和免费优先策略的账号在途名额已满，请稍后重试",
            "type": "service_unavailable", "code": "credential_concurrency_limit"}})

    @staticmethod
    def _capacity_key(entry):
        return entry.get("account_key") or entry["id"]


    def pick(self, skey: str | None, model: str | None = None, *, region=None,
             tried=(), with_capacity=False) -> CredentialManager | None:
        """Select a healthy sticky or round-robin credential, preferring eligible zero-rate accounts."""
        self._rescan()  # Reload and prune acquire their own locks.
        with self._lock:
            self._evict_sticky()
            candidates = self._candidates(model, region=region, tried=tried)
            if not candidates:
                if skey:
                    self._sticky.pop(skey, None)
                return None
            limit = CONFIG.get("max_inflight_per_account", 0)
            if with_capacity and limit:
                free = self._model_free(candidates[0], model)
                candidates = [entry for entry in candidates if self._model_free(entry, model) == free
                              and self._capacity.count(self._capacity_key(entry)) < limit]
                if not candidates:
                    raise self._capacity_error()
            best = candidates[0]
            free = self._model_free(best, model)
            top = [e for e in candidates if self._model_free(e, model) == free
                   and self._expiry_rank(e) == self._expiry_rank(best)]
            if skey and skey in self._sticky:
                cid, _ = self._sticky[skey]
                sticky = next((e for e in top if e["id"] == cid), None)
                if sticky is not None:
                    self._sticky[skey] = (cid, time.time())
                    self._sticky.move_to_end(skey)
                    return sticky["cm"]
            e = top[self._rr[region] % len(top)]
            self._rr[region] += 1
            if skey:
                self._sticky[skey] = (e["id"], time.time())
            return e["cm"]

    def headers_for(self, skey: str | None, model: str | None = None, *, region=None,
                    with_generation=False, tried=(), with_capacity=False):
        """Recheck identity and atomically reserve account capacity before sending."""
        capacity_race = False
        for _ in range(max(1, len(self._entries))):
            cm = self.pick(skey, model, region=region, tried=tried, with_capacity=with_capacity)
            if cm is None:
                return None
            reason = None
            with cm._lock:
                try:
                    headers = cm.get_headers()
                    profile = profile_for_headers(headers)
                    generation = cm._generation
                except Exception as error:
                    generation, reason = cm._generation, str(error)
            if reason is not None:
                self.cooldown(cm, reason=reason, generation=generation)
                continue
            with self._lock:
                self.reload([cm.path], reset=False)
                entry = next((entry for entry in self._entries if entry["cm"] is cm), None)
                if (entry is not None and cm._generation == generation and self._healthy(entry)
                        and self._eligible(entry, model, region=region, profile=profile) and self._model_healthy(entry, model)):
                    if with_capacity:
                        lease = self._capacity.acquire(self._capacity_key(entry),
                            CONFIG.get("max_inflight_per_account", 0), cm, generation)
                        if lease is None:
                            capacity_race = True
                            continue
                        return lease, headers
                    return ((cm, generation) if with_generation else cm), headers
        if capacity_race:
            raise self._capacity_error()
        return None

    @staticmethod
    def _lease_matches(cm, generation):
        if generation is None:
            return True
        try:
            cm._load_if_stale()
        except (OSError, ValueError):
            return generation == cm._generation
        return generation == cm._generation

    def cooldown(self, cm: CredentialManager, reason: str = "", *, generation=None):
        with self._lock, (cm._lock if generation is not None else nullcontext()):
            if not self._lease_matches(cm, generation):
                return
            for e in self._entries:
                if e["cm"] is cm:
                    e["fail_until"] = time.time() + CRED_COOLDOWN
                    e["last_error"] = sanitize_log_text(reason, 256)
                    e["last_failure_at"] = time.time()
        _log(f"[cred] 凭证熔断 {CRED_COOLDOWN}s: {Path(cm.path).name} {reason}")

    def note_status(self, cm: CredentialManager | None, status: int,
                    model: str | None = None, raw: bytes = b"", *, generation=None, retry_after=None):
        """Apply credential-wide auth cooldowns, per-model 429 cooldowns and backend/model backoff."""
        if cm is None:
            return
        if status in (401, 403):
            self.cooldown(cm, reason=f"backend HTTP {status}", generation=generation)
            return
        not_servable = _parse_not_servable(raw, status) if model else None
        if not_servable:
            self.note_not_servable(cm, model, code=not_servable[0], msg=not_servable[1])
            return
        if status != 429 or not model:
            return
        now = time.time()
        if retry_after is not None:
            until = now + retry_after
        else:
            reset = _parse_reset_time(raw)
            until = reset if reset is not None and reset > now else now + MODEL_COOLDOWN
        until = min(until, now + MODEL_COOLDOWN_MAX)
        with self._lock, (cm._lock if generation is not None else nullcontext()):
            if not self._lease_matches(cm, generation):
                return
            self._model_fail = {k: v for k, v in self._model_fail.items() if v > now}
            for e in self._entries:
                if e["cm"] is cm:
                    routed_model = _upstream_model(model, self._entry_profile(e))
                    key = (e["id"], routed_model)
                    until = max(until, self._model_fail.get(key, 0.0))
                    self._model_fail[key] = until
        _log(f"[cred] 模型冷却 {model} @ {Path(cm.path).name} 至 "
             f"{time.strftime('%m-%d %H:%M:%S', time.localtime(until))} (HTTP 429)")

    def model_cooldown_until(self, model: str | None, *, region=None) -> float | None:
        """Return the earliest reset only when all healthy credentials are cooling down."""
        if not model:
            return None
        with self._lock:
            now = time.time()
            pool = [entry for entry in self._entries if self._healthy(entry)
                    and self._eligible(entry, model, region=region)]
            if not pool:
                return None
            untils = [self._model_fail.get((entry["id"], _upstream_model(model, self._entry_profile(entry))), 0.0)
                      for entry in pool]
            if any(now >= u for u in untils):
                return None
            return min(untils)

    def note_not_servable(self, cm, model: str, code: str = "", msg: str = "") -> float:
        """Block an unsupported backend/model pair and return its retry time."""
        if not model:
            return 0.0
        entry = next((e for e in self._entries if e["cm"] is cm), None)
        endpoint = self._entry_endpoint(entry) if entry else None
        if not endpoint:
            return 0.0
        row = self._blocks.note(endpoint, _block_model(model), code=code, msg=msg)
        until = float(row.get("until") or 0.0)
        _log(f"[block] 模型 {model} @{endpoint} 官方回 {code}，"
             f"{time.strftime('%m-%d %H:%M', time.localtime(until))} 前不再派发 "
             f"(第 {row.get('hits')} 次){' | ' + msg[:80] if msg else ''}")
        return until

    def note_model_ok(self, cm, model: str) -> bool:
        """Clear model backoff immediately after a successful backend response."""
        if not model:
            return False
        entry = next((e for e in self._entries if e["cm"] is cm), None)
        endpoint = self._entry_endpoint(entry) if entry else None
        return bool(endpoint) and self._blocks.clear(endpoint, _block_model(model))

    def model_block_until(self, model: str | None, *, region=None) -> float | None:
        """Return a retry time only when every potential backend has confirmed backoff."""
        if not model:
            return None
        now = time.time()
        with self._lock:
            candidates = [e for e in self._entries
                          if self._healthy(e) and (region is None
                                                   or _in_region(self._entry_profile(e), region))]
            endpoints = {self._entry_endpoint(e) for e in candidates}
            # Unknown catalogs remain potential sources, but cannot authorize dispatch.
            capable = {self._entry_endpoint(e) for e in candidates
                       if (profile := self._entry_profile(e))
                       and profile in _model_profiles(model, profile_region(profile))}
            accounts = CONFIG.get("account_catalogs")
            def catalog_unknown(entry):
                profile = self._entry_profile(entry)
                if not profile:
                    return False
                if accounts is not None or CONFIG.get("model_cache") is not None:
                    account = (accounts or {}).get(entry.get("account_key")) or {}
                    return (account.get("profile") != profile
                            or _account_scope(account, "serves") is None)
                return _catalog_for(profile, "serves") is None
            unknown = {self._entry_endpoint(e) for e in candidates if catalog_unknown(e)}
        endpoints.discard(None)
        endpoints &= capable | unknown
        if not endpoints:
            return None
        routed = _block_model(model)
        untils = [self._blocks.until(endpoint, routed) for endpoint in endpoints]
        if any(until <= now for until in untils):
            return None
        return max(untils)

    def model_blocks_detail(self) -> list:
        """Return model backoff details for diagnostics."""
        return self._blocks.detail()

    def refresh_due(self, margin_s: int = CRED_REFRESH_MARGIN, keepalive_s: int = CRED_KEEPALIVE_S):
        """Refresh expiring or idle tokens with generation-scoped failure backoff."""
        with self._lock:
            entries = list(self._entries)
        now = time.time()
        for entry in entries:
            if now < entry.get("fail_until", 0.0) or not model_policy.credential_enabled(CONFIG, entry):
                continue
            cm = entry["cm"]
            failure = None
            refreshed = keepalive_due = False
            with cm._lock:
                try:
                    summary = cm.summary()
                    exp = (summary.get("token_expires_at") or 0) / 1000
                    last = (summary.get("last_refresh_time") or 0) / 1000
                    expiry_due = bool(summary.get("token_expired") or (exp and exp - now < margin_s))
                    keepalive_due = (not expiry_due and keepalive_s > 0
                                     and now >= entry.get("keepalive_after", 0.0)
                                     and (last <= 0 or now - last >= keepalive_s))
                    if not (expiry_due or keepalive_due):
                        continue
                    refreshed = cm.refresh_if_due(margin_s, keepalive_s if keepalive_due else 0)
                    entry["keepalive_after"] = 0.0
                except Exception as error:
                    failure = (str(error), cm._generation)
                    if keepalive_due:
                        entry["keepalive_after"] = now + CRED_KEEPALIVE_RETRY_S
            if failure:
                self.cooldown(cm, reason=failure[0], generation=failure[1])
            elif refreshed:
                _log(f"[cred] {'每日保活刷新' if keepalive_due else '已主动刷新'}并回写: {Path(entry['id']).name}")

    def remove_file(self, name: str) -> bool:
        """Share the refresh lock so an in-flight refresh cannot recreate a deleted file."""
        with self._lock:
            entry = next((x for x in self._entries if os.path.basename(x["id"]) == name), None)
            if entry is None:
                return False
            cm = entry["cm"]
            try:
                with cm._lock, credential_file_lock(cm.path.parent, cm.path.name):
                    os.unlink(entry["id"])
                    cm.invalidate()
            except FileNotFoundError:
                pass
            except OSError:
                return False
            self.prune()
            return True

    def first(self) -> CredentialManager | None:
        with self._lock:
            return self._entries[0]["cm"] if self._entries else None

    def snapshot(self) -> list[dict]:
        with self._lock:
            now = time.time()
            out = []
            for e in self._entries:
                s: dict = {"auth_file": e["id"], "healthy": self._healthy(e),
                           "in_flight": self._capacity.count(self._capacity_key(e)),
                           "max_in_flight": CONFIG.get("max_inflight_per_account", 0),
                           "model_cooldowns": {m: time.strftime("%m-%d %H:%M:%S", time.localtime(u))
                                               for (cid, m), u in self._model_fail.items()
                                               if cid == e["id"] and u > now},
                           "sticky_sessions": sum(1 for _, (cid, ts) in self._sticky.items()
                                                  if cid == e["id"] and now - ts <= STICKY_TTL)}
                try:
                    s.update(e["cm"].summary())
                except Exception:
                    s["error"] = "凭据读取失败"
                out.append(s)
            return out


def _refresher_loop(pool: CredentialPool):
    """Refresh idle credentials before expiry and persist renewed tokens."""
    while True:
        time.sleep(60)
        try:
            pool.refresh_due()
        except Exception as e:
            _log(f"[cred] 刷新线程异常: {e}")

CHECKIN_FIRST_DELAY = 30     # Initial check-in delay in seconds
HOUSEKEEP_INTERVAL = 3600    # Account maintenance interval in seconds


def _bearer_token(headers: dict) -> str:
    return (headers.get("Authorization") or "").removeprefix("Bearer ").strip()


_HOUSEKEEP_LOCK = threading.Lock()


def _sync_error(pool, ledger, entry, generation, phase, error):
    message = f"{phase}: {_network_error_text(error)}"
    pool.apply_if_current(entry["cm"], generation, lambda: ledger.note_error(entry["id"], message))
    _log(f"[{phase}] {Path(entry['id']).name} 同步失败（保留旧数据）: {message}")


def _buddy_context(entry, headers, consent_revision=None):
    def select_model(requested=None):
        from app.audit_store import safe_label
        pool = CONFIG.get("cred_pool")
        account = (CONFIG.get("account_catalogs") or {}).get(entry.get("account_key")) or {}
        if pool is None or entry.get("profile") != "cn-work" or account.get("profile") != "cn-work":
            return None
        with pool._lock:
            current = next((item for item in pool._entries if item["cm"] is entry["cm"]
                            and item.get("account_key") == entry.get("account_key")), None)
            if current is None or not pool._healthy(current):
                return None
            candidates = []
            for item in _usable_models(_account_scope(account, "serves")):
                model = item["id"]
                rate = _multiplier_value(item.get("credits"))
                if (not safe_label(model) or model in {".", ".."} or requested and model != requested
                        or rate is None or not 0 <= rate < float("inf") or "custom" in (item.get("tags") or [])):
                    continue
                rule = model_policy.rule_for(CONFIG, model)
                if (rule["upstream_id"] != model or not pool._eligible(current, model, profile="cn-work", rule=rule)
                        or not pool._model_healthy(current, model) or not pool._model_servable(current, model)):
                    continue
                name = item.get("name")
                candidates.append((rate, model, name if isinstance(name, str) and len(name) <= 160 else model))
            if not candidates:
                return None
            _, model, name = min(candidates)
            return {"id": model, "name": name}
    return buddy.context(CONFIG, entry, consent_revision, headers=headers, task_model=select_model)


def _sync_credits(pool, ledger, entry, *, checkin, failed, expected_identity=None):
    if not model_policy.credential_enabled(CONFIG, entry):
        return None
    cm, cid = entry["cm"], entry["id"]
    generation = None
    try:
        with cm._lock:
            try:
                if expected_identity is not None and cm.summary().get("account_key") != expected_identity:
                    failed.add(cid)
                    return None
                headers = cm.get_headers()
            finally:
                generation = cm._generation
        profile = profile_for_headers(headers)
        identity = account_key(profile, headers.get("X-User-Id"), headers.get("X-Enterprise-Id"))
        if entry.get("account_key") and entry["account_key"] != identity:
            failed.add(cid)
            return None  # A path now owned by another account must be rescheduled with its own preferences.
        site = site_for_headers(headers)
        token, uid, domain = _bearer_token(headers), headers.get("X-User-Id", ""), headers.get("X-Domain", "")
        day = time.strftime("%Y-%m-%d")
        if checkin and model_policy.credential_auto_checkin(CONFIG, entry) and not ledger.checkin_done(cid, day):
            try:
                def can_claim():
                    return (model_policy.credential_auto_checkin(CONFIG, entry)
                            and pool.apply_if_current(cm, generation, lambda: None))
                result = checkin_service.perform(token, uid=uid, domain=domain, can_claim=can_claim)
                if result["state"] == "cancelled":
                    if not pool.apply_if_current(cm, generation, lambda: None):
                        failed.add(cid)
                        return None
                    # A preference-only cancellation must not interrupt balance refresh.
                elif not pool.apply_if_current(cm, generation, lambda: ledger.mark_checkin(
                        cid, day, result["ok"], result.get("code"), result["message"], state=result["state"])):
                    failed.add(cid)
                    return None
                _log(f"[checkin] {Path(cid).name}: ok={result['ok']} already={result.get('already')} code={result.get('code')}")
            except Exception as error:
                _sync_error(pool, ledger, entry, generation, "checkin", error)
        if not model_policy.credential_enabled(CONFIG, entry):
            return None
        if checkin and model_policy.credential_auto_travel(CONFIG, entry):
            try:
                def can_travel():
                    return (model_policy.credential_auto_travel(CONFIG, entry)
                            and pool.apply_if_current(cm, generation, lambda: None))
                trip = travel.perform(token, profile_for_headers(headers), can_write=can_travel,
                                      buddy_context=_buddy_context(entry, headers))
                if not pool.apply_if_current(cm, generation, lambda: travel.remember(ledger, cid, trip)):
                    failed.add(cid)
                    return None
                buddy.daily_warning(CONFIG, entry.get("account_key"), entry.get("profile"), trip)
            except Exception as error:
                _sync_error(pool, ledger, entry, generation, "travel", error)
        if not model_policy.credential_enabled(CONFIG, entry):
            return None
        balance = credits_mod.fetch_credits(token, uid=uid, domain=domain)
        if bool(balance.get("intl")) != (site == INTERNATIONAL):
            raise ValueError("积分响应与凭据站点不一致")
        if not pool.apply_if_current(cm, generation, lambda: ledger.update_credits(cid, balance)):
            failed.add(cid)
            return None
        _log(f"[credits] {Path(cid).name}: 站点 {site}，余额 {balance['credits']}")
        profile = profile_for_headers(headers)
        return entry, generation, headers, profile
    except Exception as error:
        failed.add(cid)
        _sync_error(pool, ledger, entry, generation, "credits", error)
        return None


def _publish_model_cache():
    """Publish account-scoped versioned catalogs, excluding ownerless shared caches."""
    cache = CONFIG.get("model_cache")
    if cache is not None:
        pool = CONFIG.get("cred_pool")
        with pool._lock if pool is not None else nullcontext():
            accounts = {}
            for entry in pool.entries() if pool is not None else []:
                identity, profile = entry.get("account_key"), entry.get("profile")
                if not identity or not profile:
                    continue
                key = catalog_cache_key(profile, identity)
                known = cache.age(key) is not None
                accounts[identity] = {"profile": profile,
                                      "models": cache.models(key) if known else None,
                                      "serves": cache.serves(key) if known else None}
            CONFIG["account_catalogs"] = accounts
            catalogs = {profile: None for profile in PROFILE_ENDPOINTS}
            for account in accounts.values():
                if account["models"] is not None:
                    models = catalogs[account["profile"]]
                    if models is None:
                        models = catalogs[account["profile"]] = []
                    models.extend(account["models"])
            CONFIG["model_catalogs"] = catalogs
            CONFIG["models_remote"], CONFIG["models_intl"] = catalogs["cn-cli"], catalogs["intl-cli"]
    invalidate_model_table()


def _sync_model_catalogs(pool, ledger, refs, failed):
    cache = CONFIG.get("model_cache")
    if cache is None:
        return
    for entry, generation, headers, profile in refs.values():
        identity = account_key(profile, headers.get("X-User-Id"), headers.get("X-Enterprise-Id"))
        if not model_policy.credential_enabled(CONFIG, entry):
            continue
        key = catalog_cache_key(profile, identity)
        if cache.fresh(key) and not entry.get("catalog_dirty"):
            continue
        try:
            scopes = credits_mod.fetch_model_scopes(
                _bearer_token(headers), domain=headers.get("X-Domain", ""),
                uid=headers.get("X-User-Id", ""), enterprise_id=headers.get("X-Enterprise-Id", ""))
            models, serves = scopes["picker"], scopes["account"]
            def publish():
                cache.put(key, models, serves=serves)
                for current in pool._entries:
                    if current["cm"] is entry["cm"]:
                        current["catalog_dirty"] = False
            if pool.apply_if_current(entry["cm"], generation, publish):
                _log(f"[models] {profile} 模型表已刷新: 选择器 {len(models)} 个，"
                     f"账号根表 {len(serves)} 个")
            else:
                failed.add(entry["id"])
        except Exception as error:
            failed.add(entry["id"])
            _sync_error(pool, ledger, entry, generation, "models", error)
    _publish_model_cache()


def _sync_usage(pool, entries=None, expected_identity=None):
    """Refresh usage during maintenance with independent per-account snapshots."""
    accounts = CONFIG.get("usage_daily_accounts")
    if not isinstance(accounts, dict):
        accounts = CONFIG["usage_daily_accounts"] = {}
    targets = pool.entries() if entries is None else entries
    target_ids = {entry["id"] for entry in targets}
    previous_stale = set((CONFIG.get("usage_daily") or {}).get("stale_accounts", []))
    stale = {entry["id"] for entry in pool.entries()
             if entry["id"] not in target_ids and Path(entry["id"]).name in previous_stale}
    for entry in targets:
        if not model_policy.credential_enabled(CONFIG, entry):
            continue
        try:
            cm = entry["cm"]
            with cm._lock:
                if expected_identity is not None and cm.summary().get("account_key") != expected_identity:
                    stale.add(entry["id"])
                    continue
                headers = cm.get_headers()
                generation = cm._generation
            site = site_for_headers(headers)
            usage = credits_mod.fetch_request_usage(_bearer_token(headers), uid=headers.get("X-User-Id", ""),
                                                    domain=headers.get("X-Domain", ""))
            def store():
                accounts[entry["id"]] = {"site": site, "by_day": usage["by_day"],
                                         "total_credits": round(usage["total_credits"], 2),
                                         "requests": usage["requests"],
                                         "partial": bool(usage.get("partial")),
                                         "fetched_at": time.time()}
            if not pool.apply_if_current(cm, generation, store):
                stale.add(entry["id"])
        except Exception as error:
            stale.add(entry["id"])
            _log(f"[usage] {Path(entry['id']).name} 明细拉取失败（保留其上次成功快照）: {_network_error_text(error)}")
    _publish_usage_daily(pool, stale)
    return stale & target_ids


def _publish_usage_daily(pool, stale=()):
    """Aggregate enabled accounts' usage, retaining failed snapshots with explicit staleness."""
    accounts = CONFIG.get("usage_daily_accounts")
    if not isinstance(accounts, dict):
        accounts = {}
    enabled = {e["id"] for e in pool.entries() if model_policy.credential_enabled(CONFIG, e)}
    by_day, groups = {}, {}
    used, count = 0.0, 0
    partial = False
    newest = 0.0
    stale_out = []
    for cred_id, snap in accounts.items():
        if cred_id not in enabled:
            continue
        site = snap.get("site") or "domestic"
        group = groups.setdefault(site, {"by_day": {}, "total_credits": 0.0, "requests": 0})
        for day, models in (snap.get("by_day") or {}).items():
            total_day = by_day.setdefault(day, {})
            site_day = group["by_day"].setdefault(day, {})
            for model, credit in models.items():
                total_day[model] = round(total_day.get(model, 0.0) + credit, 6)
                site_day[model] = round(site_day.get(model, 0.0) + credit, 6)
        group["total_credits"] += float(snap.get("total_credits") or 0)
        group["requests"] += int(snap.get("requests") or 0)
        used += float(snap.get("total_credits") or 0)
        count += int(snap.get("requests") or 0)
        newest = max(newest, float(snap.get("fetched_at") or 0))
        if snap.get("partial"):
            partial = True
    # Failed enabled accounts must remain visible even without a prior snapshot.
    for cred_id in stale:
        if cred_id in enabled:
            partial = True
            stale_out.append(Path(cred_id).name)
    # A zero timestamp preserves quota-difference fallback when no usage snapshot exists.
    for group in groups.values():
        group["total_credits"] = round(group["total_credits"], 2)
    out = {"by_day": by_day, "groups": groups, "total_credits": round(used, 2),
           "requests": count, "fetched_at": newest, "partial": partial}
    if stale_out:
        out["stale_accounts"] = sorted(stale_out)
    CONFIG["usage_daily"] = out
    _log(f"[usage] 明细已同步: {count} 请求 / {used:.2f} credits"
         + (f" | {len(stale_out)} 账号同步失败" if stale_out else ""))


def _housekeep_once(pool: CredentialPool, ledger, *, pending_only=False):
    """Serialize generation-scoped maintenance; new credentials only trigger balance/catalog reads."""
    if credits_mod is None:
        return
    with _HOUSEKEEP_LOCK:
        pool._rescan()
        ids = pool.begin_sync(all_entries=not pending_only)
        failed = set()
        try:
            refs = {}
            for entry in pool.entries():
                if entry["id"] not in ids:
                    continue
                result = _sync_credits(pool, ledger, entry, checkin=not pending_only, failed=failed)
                if result is not None:
                    refs[entry["id"]] = result
            _sync_model_catalogs(pool, ledger, refs, failed)
            if not pending_only:
                _sync_usage(pool)
        except Exception:
            failed.update(ids)
            raise
        finally:
            pool.end_sync(ids, failed)


def _housekeeper_loop(pool: CredentialPool, ledger) -> None:
    """Wake for new credentials, retry failed work with backoff, and run hourly maintenance."""
    next_full = time.monotonic() + CHECKIN_FIRST_DELAY
    while True:
        pool._sync_event.wait(pool.sync_wait(next_full - time.monotonic()))
        full_due = time.monotonic() >= next_full
        try:
            _housekeep_once(pool, ledger, pending_only=not full_due)
        except Exception as error:
            _log(f"[housekeeper] 循环异常: {_network_error_text(error)}")
        if full_due:
            next_full = time.monotonic() + HOUSEKEEP_INTERVAL


# ---------------------------------------------------------------------------
# Model inventory
# ---------------------------------------------------------------------------

# Fallback models for legacy domestic deployments without a cloud catalog.
DEFAULT_MODELS = [
    "hy4-preview", "hy4-preview-x",
    "hy3", "hy3-x",
    "deepseek-v4-pro", "deepseek-v4-flash", "deepseek-v4.1-flash", "deepseek-v3-2-volc",
    "glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-5.1", "glm-5.0", "glm-5.0-turbo",
    "glm-5v-turbo", "glm-4.7", "glm-4.6", "glm-4.6v",
    "minimax-m3", "minimax-m2.7", "minimax-m2.5",
    "kimi-k3-1", "kimi-k2.7", "kimi-k2.6", "kimi-k2.5", "kimi-k2-thinking",
    "hunyuan-chat", "default",
    "auto",  # Backend-selected model alias
]


# Supported optional upstream request fields.
PASSTHROUGH_BODY_KEYS = {
    "model", "messages", "tools", "tool_choice", "temperature",
    "max_tokens", "max_completion_tokens", "top_p", "stream",
    "stream_options", "stop", "presence_penalty", "frequency_penalty",
    "n", "response_format", "seed", "user", "reasoning_effort", "prompt_cache_key",
    "verbosity", "reasoning_summary", "parallel_tool_calls",
}

# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(title="codebuddy2api", version=APP_VERSION, lifespan=inference_lifespan)
app.add_middleware(InferenceResourcesMiddleware, config=lambda: CONFIG)

# Anthropic error types: https://platform.claude.com/docs/en/api/errors
_ANTHROPIC_ERROR_TYPES = {
    "auth_error": "authentication_error",
    "rate_limit_error": "rate_limit_error",
    "invalid_request_error": "invalid_request_error",
    "not_found_error": "not_found_error",
    "upstream_error": "api_error",
}


@app.exception_handler(HTTPException)
async def _protocol_http_exception(request: Request, exc: HTTPException):
    """Shape /v1 errors for the client protocol; retain FastAPI defaults elsewhere."""
    path = request.url.path
    if not path.startswith("/v1/"):
        return await _default_http_exception_handler(request, exc)
    detail = exc.detail
    err = detail.get("error") if isinstance(detail, dict) else None
    if not isinstance(err, dict):
        err = {"message": str(detail), "type": "error"}
    message = str(err.get("message") or "")
    if path.startswith("/v1/messages"):
        # Preserve upstream business codes in Anthropic error envelopes.
        etype = _ANTHROPIC_ERROR_TYPES.get(str(err.get("type") or ""))
        if exc.status_code == 404:
            etype = "not_found_error"  # Anthropic's required type for HTTP 404.
        elif etype is None:
            etype = "api_error" if exc.status_code >= 500 else "invalid_request_error"
        error_obj = {**err, "type": etype, "message": message}  # Retain structured error fields.
        return JSONResponse({"type": "error", "error": error_obj},
                            status_code=exc.status_code, headers=exc.headers)
    # OpenAI uses a top-level error object.
    body = {"error": {**err, "message": message}}
    return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)
CONFIG: dict = {"api_key": "", "cred": None, "log_path": None, "ledger": None,
                "admin_csrf": True,     # Startup-only Origin/CSRF policy
                "models_remote": None,   # Domestic cloud model inventory
                "models_intl": None,     # Eligible international model inventory
                "model_cache": None,     # Versioned catalog cache
                "model_catalogs": {},   # Display-only merged product catalogs
                "account_catalogs": None,  # Account-scoped models/serves; None enables legacy embedding
                "trial_ledger": None,
                "model_guard": True,     # Reject models absent from authorized catalogs
                "max_images": 16, "image_policy": "truncate",
                "max_request_bytes": 32 * 1024 * 1024, "log_body_limit": 65536,
                "max_inbound_bytes": 64 * 1024 * 1024,
                "max_collect_bytes": 8 * 1024 * 1024, "max_concurrent": 64,
                "upstream_keepalive": False, "max_inflight_per_account": 0,
                "request_context_mode": "legacy",
                "failover_max": 0,     # Credential failovers allowed before the first response byte
                "retry_write_timeout": False,  # Opt-in replay after incomplete writes
                "usage_daily": None,     # Usage aggregated by date and model
                "usage_daily_accounts": None,  # Independent per-account usage snapshots
                "credit_price_cny": None, "credit_price_usd": None, "usd_rate": None,
                "desensitize": False, "no_compact": False, "keep_tool_metadata": False}  # None prices use module defaults.

# In-memory OAuth sessions do not survive restarts.
_OAUTH = auth_oauth.OAuthManager(user_agent=USER_AGENT)


# ---------------------------------------------------------------------------
# File logging
# ---------------------------------------------------------------------------

_LOG_LOCK = threading.Lock()
LOG_MAX_BYTES = 50 * 1024 * 1024  # Rotation threshold; a single entry may exceed it.
LOG_BACKUPS = 2                    # Retain the two most recent rotated logs.


def _log(msg: str):
    """Write bounded redacted logs with rotation under a shared lock."""
    audit = CONFIG.get("audit_store")
    component = re.match(r"\[(cred|credits|models|usage|trial|checkin|housekeeper)\]", msg)
    if audit is not None and component:
        # Persist event codes, not free-form lines which may contain upstream data.
        code = "cooldown" if "熔断" in msg or "冷却" in msg else "failure" if "失败" in msg or "异常" in msg else "updated"
        audit.event("runtime", component.group(1), {"code": code})
    path = CONFIG.get("log_path")
    if not path:
        return
    budget = min(max(1024, CONFIG.get("log_body_limit", 65536) + 256), max(0, LOG_MAX_BYTES - 256))
    msg = sanitize_log_text(msg, budget)
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    try:
        with _LOG_LOCK:
            try:
                size = os.path.getsize(path)
            except FileNotFoundError:
                size = 0
            rotated = size > 0 and size + len(line.encode("utf-8")) > LOG_MAX_BYTES
            if rotated:
                for i in range(LOG_BACKUPS - 1, 0, -1):
                    old = f"{path}.{i}"
                    if os.path.exists(old):
                        os.replace(old, f"{path}.{i + 1}")
                os.replace(path, f"{path}.1")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8", newline="\n") as stream:
                if rotated:
                    stream.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ==== 日志轮转 ====\n")
                stream.write(line)
    except OSError:
        pass  # Logging failures must not interrupt requests.


def _log_json(label: str, value):
    if CONFIG.get("log_path") and CONFIG.get("log_body_limit", 65536):
        _log(f"{label}\n{format_log_body(value, CONFIG.get('log_body_limit', 65536))}")


def _log_text_body(label: str, text: str):
    if CONFIG.get("log_path") and CONFIG.get("log_body_limit", 65536):
        _log(f"{label}\n{sanitize_log_text(text, CONFIG.get('log_body_limit', 65536))}")




def _truncate(s: str, n: int = 80) -> str:
    s = str(s).replace("\n", " ").strip()
    return s[:n] + ("…" if len(s) > n else "")


def _check_auth(authorization: Optional[str], x_api_key: Optional[str]):
    require_api_key(CONFIG["api_key"], authorization, x_api_key)


def _check_admin_auth(authorization: Optional[str], x_api_key: Optional[str]):
    if not CONFIG.get("api_key"):
        raise HTTPException(status_code=503, detail={"error": {"message": "管理接口需要配置 API key",
                                                             "type": "management_locked"}})
    _check_auth(authorization, x_api_key)


def _cred_for(payload: dict, model: str | None = None, *, region=None, tried=()):
    """Select a fresh credential lease and headers, excluding tried accounts; report unavailable capacity."""
    context = current_context()
    raw_key = context.session_key if context is not None and context.scoped else session_key(payload)
    skey = f"{region}:{raw_key}" if raw_key and region is not None else raw_key
    skey = model_policy.sticky_scope(CONFIG, skey, model)
    pool = CONFIG.get("cred_pool")
    if pool is not None:
        resources = request_resources.get()
        picked = pool.headers_for(skey, model, region=region, with_generation=True, tried=tried,
                                  with_capacity=resources is not None)
        if picked is None:
            until = pool.model_cooldown_until(model, region=region)
            if until:
                t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(until))
                raise HTTPException(status_code=429,
                                    headers={"Retry-After": str(max(1, math.ceil(until - time.time())))},
                                    detail={"error": {
                    "message": f"模型 {model} 额度冷却中（全部凭证），预计 {t} 重置后恢复",
                    "type": "rate_limit_error"}})
            blocked = pool.model_block_until(model, region=region)
            if blocked:
                # Report confirmed unsupported models as HTTP 404.
                t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(blocked))
                raise HTTPException(status_code=404, detail={"error": {
                    "message": f"模型 {model} 在当前所有已登录后端均不可用（官方回 service info not found），"
                               f"预计 {t} 后重试；请改用 /v1/models 列出的模型",
                    "type": "invalid_request_error", "code": "model_not_found",
                    "param": "model"}})
            raise HTTPException(status_code=503, headers={"Retry-After": "3" if _catalog_pending(region) else "30"},
                                detail={"error": {"message": "无可用凭证（未登录、目录/额度未就绪或全部熔断）",
                                                  "type": "auth_error"}})
        cm, headers = picked
        if resources is not None:
            resources.add(cm)
    else:
        cm = CONFIG["cred"]
        if cm is None or cm in {_cred_manager(item) for item in tried}:
            raise HTTPException(status_code=503, detail={"error": {"message": "未找到登录凭据，请先在桌面端登录 CodeBuddy/WorkBuddy", "type": "auth_error"}})
        with cm._lock:
            headers = cm.get_headers()
            cm = (cm, cm._generation)
    profile = profile_for_headers(headers)
    if not _in_region(profile, region):
        raise HTTPException(status_code=503, detail={"error": {"message": "未找到指定地域凭据", "type": "auth_error"}})
    if context is not None and context.scoped:
        identity = account_key(profile, headers.get("X-User-Id"), headers.get("X-Enterprise-Id"))
        headers["X-Conversation-ID"] = context.conversation_id(profile, identity)
    else:
        headers.update(_dynamic_request_headers(f"{profile}:{skey}" if skey else None))
    return cm, headers


def _route_chat(payload, body, rid, *, tried=()):
    """Resolve the account's backend, region and model without changing client URLs."""
    cred, headers = _cred_for(payload, body.get("model"), tried=tried)
    profile = profile_for_headers(headers)
    routed_model = _upstream_model(body.get("model"), profile)
    if routed_model != body.get("model"):
        body = {**body, "model": routed_model}
        _guard_request_size(body)
    url = chat_url_for_headers(headers)
    observe_route(public_model=payload.get("model", "auto"), upstream_model=routed_model,
                  profile=profile, credential=account_key(profile, headers.get("X-User-Id"),
                                                         headers.get("X-Enterprise-Id")))
    _log(f"[{rid}] ROUTE | region={profile_region(profile)} | profile={profile} | model={routed_model} | url={url}")
    return body, cred, headers, url


def _note_cred_model_ok(cred, model: str | None) -> None:
    """Clear backend/model backoff after an upstream HTTP 200 response."""
    pool = CONFIG.get("cred_pool")
    if pool is not None and cred is not None and model:
        cm = cred[0] if isinstance(cred, tuple) else cred
        pool.note_model_ok(cm, model)


def _note_cred_status(cred, status: int, model: str | None = None, raw: bytes = b"", *, retry_after=None):
    """Record generation-scoped authentication, quota and unsupported-model failures."""
    pool = CONFIG.get("cred_pool")
    if pool is not None and cred is not None:
        cm, generation = cred if isinstance(cred, tuple) else (cred, None)
        pool.note_status(cm, status, model=model, raw=raw, generation=generation, retry_after=retry_after)

@app.get("/health")
def health():
    """Return public liveness without accessing or exposing credentials."""
    return {"status": "ok"}


@app.get("/admin/credentials")
def admin_list_credentials(authorization: Optional[str] = Header(default=None),
                           x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Return account expiry, health and session-binding metadata."""
    _check_admin_auth(authorization, x_api_key)
    pool = CONFIG.get("cred_pool")
    if CONFIG.get("management") is not None:
        return {"credentials": CONFIG["management"].admin_credential_inventory()}
    return {"credentials": pool.snapshot() if pool else []}


class CredentialConflictError(CredentialFileError):
    """Signal that another credential file already owns the account."""


def _store_credential(directory: Path, name: str, content: bytes, uid: str, *, replace_identity=True,
                      replace_existing=True) -> Path:
    """Serialize imports and logins with background refresh and standalone CLI writes."""
    pool = CONFIG.get("cred_pool")
    identity = _credential_identity(json.loads(content))
    target = directory.resolve() / name
    with pool._lock if pool is not None else nullcontext():
        cm = None
        if pool is not None:
            pool._rescan()
            holder = pool.find_by_uid(uid, identity)
            if holder and holder != str(target):
                raise CredentialConflictError("该账号已在凭证池中")
            entry = next((entry for entry in pool._entries if entry["id"] == str(target)), None)
            cm = entry["cm"] if entry else None
        elif CONFIG.get("cred") is not None and CONFIG["cred"].path.resolve() == target:
            cm = CONFIG["cred"]
        with cm._lock if cm is not None else nullcontext():
            with credential_file_lock(directory, name):
                if not replace_existing and target.exists():
                    raise CredentialConflictError("文件已存在，需明确允许替换")
                if not replace_identity and target.exists() and _cred_identity(target) != identity:
                    raise CredentialConflictError("OAuth 不可覆盖其他产品或账号的凭据")
                target = atomic_write_credential(directory, name, content)
                if pool is not None:
                    pool.reload([target])
                elif cm is not None:
                    cm.invalidate()
    return target


@app.post("/admin/credentials")
async def admin_add_credential(request: Request,
                               authorization: Optional[str] = Header(default=None),
                               x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Validate and atomically import credentials from the controlled directory."""
    _check_admin_auth(authorization, x_api_key)
    try:
        body = await request.json()
    except (ValueError, UnicodeError):
        raise HTTPException(status_code=400, detail={"error": {"message": "请求体必须是 JSON 对象", "type": "invalid_request_error"}}) from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail={"error": {"message": "请求体必须是 JSON 对象", "type": "invalid_request_error"}})
    dst_dir = managed_auth_dir().resolve()
    import_dir = Path(os.environ.get("CODEBUDDY_IMPORT_DIR") or dst_dir / "imports")
    try:
        name, content = read_import_file(import_dir, body.get("path"))
        cred_data = auth_oauth.loads_strict(content.decode("utf-8"))
        src_uid, verr = auth_oauth.validate_cred_data(cred_data)
        if verr:
            raise CredentialFileError("凭据格式或站点校验失败")
        if (not isinstance(cred_data.get("account") or {}, dict)
                or not isinstance(cred_data["auth"].get("expiresAt", 0), (int, float))):
            raise CredentialFileError("凭据账号或过期时间格式无效")
        # Normalize token aliases before persistence.
        content = json.dumps(auth_oauth.normalize_cred_data(cred_data), ensure_ascii=False).encode("utf-8")
    except CredentialFileError:
        raise HTTPException(status_code=400, detail={"error": {"message": "凭据文件不符合导入要求", "type": "invalid_request_error"}}) from None
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(status_code=400, detail={"error": {"message": "凭据必须是有效的 UTF-8 JSON 对象", "type": "invalid_request_error"}}) from None
    except OSError:
        raise HTTPException(status_code=400, detail={"error": {"message": "导入目录或文件不可读", "type": "invalid_request_error"}}) from None
    try:
        dst = _store_credential(dst_dir, name, content, src_uid)
    except CredentialConflictError:
        raise HTTPException(status_code=409, detail={"error": {"message": "该账号已在池中，请使用同文件名更新或先移除旧凭据", "type": "invalid_request_error"}}) from None
    except CredentialFileError:
        raise HTTPException(status_code=400, detail={"error": {"message": "凭据文件名或保存目标不符合要求", "type": "invalid_request_error"}}) from None
    except OSError:
        raise HTTPException(status_code=500, detail={"error": {"message": "凭据保存失败", "type": "server_error"}}) from None
    return {"imported": str(dst)}


@app.delete("/admin/credentials/{name}")
def admin_del_credential(name: str,
                         authorization: Optional[str] = Header(default=None),
                         x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Delete the named .info file and remove its credential from the pool."""
    _check_admin_auth(authorization, x_api_key)
    pool = CONFIG.get("cred_pool")
    if CONFIG.get("management") is not None:
        CONFIG["management"].admin_delete_guard(os.path.basename(name))
    if pool is None or not pool.remove_file(os.path.basename(name)):
        raise HTTPException(status_code=404, detail={"error": {"message": f"凭据不在池中: {name}", "type": "invalid_request_error"}})
    return {"removed": os.path.basename(name)}



def _save_oauth_credential(cred: dict) -> Path:
    """Update credentials by product, account and tenant without crossing identities."""
    uid, error = auth_oauth.validate_cred_data(cred)
    if error:
        raise CredentialFileError("凭据格式或站点校验失败")
    dst_dir = managed_auth_dir()
    identity = _credential_identity(cred)
    profile = profile_for_auth(cred["auth"])
    target = next((f for f in sorted(dst_dir.glob("*.info")) if _cred_identity(f) == identity), None)
    existing = None
    if target is not None:
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    name = target.name if target is not None else f"{uid}.info"
    if target is None and (dst_dir / name).exists():
        name = f"{uid}-{profile}.info"
        if (dst_dir / name).exists():
            name = f"{uid}-{profile}-{identity}.info"
        if (dst_dir / name).exists():
            raise CredentialConflictError("OAuth 保存目标已被其他身份占用")
    cred = auth_oauth.merge_existing_accounts(cred, existing)
    return _store_credential(
        dst_dir, name, json.dumps(cred, ensure_ascii=False, indent=2).encode("utf-8"), uid,
        replace_identity=False)


@app.post("/admin/oauth/start")
def admin_oauth_start(site: str = "cn",
                      authorization: Optional[str] = Header(default=None),
                      x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Start OAuth and return the browser authorization URL."""
    _check_admin_auth(authorization, x_api_key)
    try:
        return _OAUTH.start(site=site)
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"error": {"message": str(e), "type": "invalid_request_error"}})
    except Exception as e:
        raise HTTPException(status_code=502, detail={"error": {"message": f"发起失败: {e}", "type": "upstream_error"}})


@app.get("/admin/oauth/poll")
def admin_oauth_poll(login_id: str = "",
                     authorization: Optional[str] = Header(default=None),
                     x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Poll OAuth and persist completed logins with pool reload."""
    _check_admin_auth(authorization, x_api_key)
    try:
        r = _OAUTH.poll(login_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail={"error": {"message": f"轮询失败: {e}", "type": "upstream_error"}})
    if not r.get("done"):
        return {"done": False}
    cred = r.get("cred")
    if r.get("error") or not cred:
        return {"done": True, "error": r.get("error") or "登录失败"}
    uid = r["uid"]
    try:
        target = _save_oauth_credential(cred)
    except CredentialFileError:
        return {"done": True, "error": "凭据格式、站点或保存目标不符合要求"}
    except OSError:
        raise HTTPException(status_code=500, detail={"error": {"message": "凭据保存失败", "type": "server_error"}}) from None
    _log(f"[oauth] 无感登录已入库: {r.get('nickname') or uid} ({uid}) -> {target.name}")
    return {"done": True, "uid": uid, "nickname": r.get("nickname") or "", "imported": str(target)}

@app.get("/admin/credits")
def admin_credits(authorization: Optional[str] = Header(default=None),
                  x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Return cached balances, credit expiry segments and check-in status."""
    _check_admin_auth(authorization, x_api_key)
    ledger = CONFIG.get("ledger")
    return {"credits": ledger.snapshot() if ledger else {}}


@app.get("/admin/model-blocks")
def admin_model_blocks(authorization: Optional[str] = Header(default=None),
                       x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Return unsupported backend/model pairs and their retry deadlines."""
    _check_admin_auth(authorization, x_api_key)
    pool = CONFIG.get("cred_pool")
    return {"model_blocks": pool.model_blocks_detail() if pool is not None else []}


@app.post("/admin/checkin")
def admin_checkin(authorization: Optional[str] = Header(default=None),
                  x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Run daily-idempotent manual check-in without implicitly syncing balances or usage."""
    _check_admin_auth(authorization, x_api_key)
    return _admin_credential_action("checkin")


def _admin_credential_action(action, identity=None, *, consent_revision=None):
    from app.credential_actions import run
    return run(sys.modules[__name__], action, identity, consent_revision=consent_revision)


@app.post("/admin/sync")
def admin_sync(authorization: Optional[str] = Header(default=None),
               x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_admin_auth(authorization, x_api_key)
    return _admin_credential_action("sync")


@app.post("/admin/credentials/{identity}/{action}")
async def admin_credential_action(identity: str, action: str, request: Request,
                                 authorization: Optional[str] = Header(default=None),
                                 x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_admin_auth(authorization, x_api_key)
    from app.admin_api import _body
    try:
        body = await _body(request, 4096, allow_empty=True)
        if body and (action != "travel" or set(body) != {"confirm_buddy", "agreement_revision"}
                     or body["confirm_buddy"] is not True or not isinstance(body["agreement_revision"], str)):
            raise ValueError()
    except ValueError:
        raise HTTPException(400, "首领确认参数无效") from None
    return await run_in_threadpool(_admin_credential_action, action, identity,
                                  consent_revision=body.get("agreement_revision"))


# ---------------------------------------------------------------------------
# OpenAI-compatible billing estimates
# ---------------------------------------------------------------------------

def _billing_totals() -> dict:
    """Convert regional balances independently, using official usage or a quota-difference fallback."""
    empty_grp = {"remaining": 0.0, "used_by_quota": 0.0, "soonest_expiry": None}
    ledger = CONFIG.get("ledger")
    snap = ledger.snapshot() if ledger else {}
    price_cny = CONFIG.get("credit_price_cny")
    price_usd = CONFIG.get("credit_price_usd")
    rate = CONFIG.get("usd_rate")
    if price_cny is None:
        price_cny = credits_mod.CREDIT_PRICE_CNY if credits_mod else 0.014
    if price_usd is None:
        price_usd = credits_mod.CREDIT_PRICE_USD if credits_mod else 0.03
    if rate is None:
        rate = credits_mod.USD_RATE_CNY if credits_mod else 7.15
    agg = (credits_mod.aggregate_credits(snap) if credits_mod else
           {"remaining": 0.0, "used_by_quota": 0.0, "soonest_expiry": None,
            "groups": {"domestic": dict(empty_grp), "international": dict(empty_grp)}})
    cache = CONFIG.get("usage_daily") or {}
    detail = bool(cache.get("fetched_at"))
    detail_groups = cache.get("groups") or {}
    per_usd = {"domestic": price_cny / rate, "international": price_usd}
    remaining = used = rem_usd = used_usd = rem_cny = 0.0
    groups_out: dict = {}
    for grp, g in (agg.get("groups") or {}).items():
        r = float(g.get("remaining") or 0)
        gd = detail_groups.get(grp)
        u = (float(gd.get("total_credits") or 0) if (detail and gd)
             else float(g.get("used_by_quota") or 0))  # Fall back to the group's quota difference.
        unit = per_usd.get(grp, 0.0)
        remaining += r
        used += u
        rem_usd += r * unit
        used_usd += u * unit
        rem_cny += r * (unit * rate if grp == "international" else price_cny)
        groups_out[grp] = {"credits_remaining": round(r, 2), "credits_used": round(u, 2),
                           "balance_usd": round(r * unit, 4),
                           "price_usd_per_credit": round(unit, 6)}
    return {"remaining": round(remaining, 2), "used": round(used, 2),
            "quota": round(remaining + used, 2),
            "remaining_usd": round(rem_usd, 4), "used_usd": round(used_usd, 4),
            "quota_usd": round(rem_usd + used_usd, 4),
            "remaining_cny": round(rem_cny, 4),
            "soonest_expiry": agg.get("soonest_expiry"),
            "price_cny": price_cny, "price_usd": price_usd, "rate": rate,
            "used_source": "official_usage_detail" if detail else "quota_delta",
            # Expose incomplete pagination or failed account synchronization.
            "partial": bool(agg.get("partial") or cache.get("partial")),
            "groups": groups_out, "by_day": cache.get("by_day") or {}}


@app.get("/v1/dashboard/billing/subscription")
def billing_subscription(authorization: Optional[str] = Header(default=None),
                         x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Expose subscription estimates with balance equal to the hard limit minus usage."""
    _check_auth(authorization, x_api_key)
    t = _billing_totals()
    limit = t["quota_usd"]
    return {
        "object": "billing_subscription",
        "has_payment_method": True, "canceled": False, "canceled_at": None, "delinquent": None,
        # Conservatively use the earliest credit expiry as the access deadline.
        "access_until": int(t["soonest_expiry"] or (time.time() + 30 * 86400)),
        "soft_limit": int(limit * 100), "hard_limit": int(limit * 100),
        "soft_limit_usd": limit, "hard_limit_usd": limit, "system_hard_limit_usd": limit,
        "plan": {"title": f"CodeBuddy Credits (CN {t['price_cny']:g} CNY/credit · "
                                        f"INTL {t['price_usd']:g} USD/credit)"},
        # Include total and regional estimates as optional response fields.
        "codebuddy_credits_remaining": t["remaining"],
        "codebuddy_credits_used": t["used"],
        "codebuddy_balance_usd": t["remaining_usd"],
        "codebuddy_balance_cny": t["remaining_cny"],
        "codebuddy_sites": t["groups"],
        # Expose incomplete balance or usage data to callers.
        "codebuddy_partial": t["partial"],
        **({"codebuddy_stale_accounts": stale} if (stale := (CONFIG.get("usage_daily") or {}).get("stale_accounts")) else {}),
    }


@app.get("/v1/dashboard/billing/usage")
def billing_usage(start_date: Optional[str] = None, end_date: Optional[str] = None,
                  authorization: Optional[str] = Header(default=None),
                  x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Return estimated usage in cents, with daily model costs for the last 30 days."""
    _check_auth(authorization, x_api_key)
    t = _billing_totals()
    # Convert each region at its own rate before combining daily and total usage.
    cents = {"domestic": t["price_cny"] / t["rate"] * 100, "international": t["price_usd"] * 100}
    detail = CONFIG.get("usage_daily") or {}
    priced: dict = {}
    for site, group in (detail.get("groups") or {}).items():
        unit = cents.get(site)
        if unit is None:
            continue
        for day, models in (group.get("by_day") or {}).items():
            slot = priced.setdefault(day, {})
            for model, credit in models.items():
                slot[model] = slot.get(model, 0.0) + float(credit) * unit
    daily = []
    for day in sorted(priced):
        if start_date and day < start_date:
            continue
        if end_date and day > end_date:
            continue
        items = [{"name": m, "cost": round(c, 4)}
                 for m, c in sorted(priced[day].items()) if c > 0]
        try:
            ts = int(time.mktime(time.strptime(day, "%Y-%m-%d")))
        except ValueError:
            ts = 0
        daily.append({"timestamp": ts, "line_items": items})
    if start_date or end_date:  # Sum only the requested interval.
        total_cents = round(sum(sum(i["cost"] for i in d["line_items"]) for d in daily), 2)
    else:                      # Preserve the subscription balance identity.
        total_cents = round(t["used_usd"] * 100, 2)
    out = {"object": "list", "total_usage": total_cents, "daily_costs": daily}
    if t.get("partial"):
        out["partial"] = True
    if detail.get("stale_accounts"):
        out["stale_accounts"] = detail["stale_accounts"]
    return out


# Prefer cloud catalogs over static fallback models.
_MODEL_TABLE_TTL = 60.0   # Model snapshot lifetime in seconds
_model_table_cache: dict = {}


def invalidate_model_table() -> None:
    """Invalidate the public model snapshot after catalog changes."""
    global _model_table_cache
    _model_table_cache = {}


def _catalog_for(profile: str, scope: str = "models"):
    """Return a profile catalog within the requested account scope."""
    accounts = CONFIG.get("account_catalogs")
    if accounts is not None or CONFIG.get("model_cache") is not None:
        pool = CONFIG.get("cred_pool")
        models = None
        for entry in pool.entries() if pool is not None else []:
            if entry.get("profile") != profile:
                continue
            account = (accounts or {}).get(entry.get("account_key")) or {}
            items = _account_scope(account, scope)
            if account.get("profile") == profile and items is not None:
                if models is None:
                    models = []
                models.extend(items)
        return models
    catalogs = CONFIG.get("model_catalogs") or {}
    if profile in catalogs:
        return catalogs[profile]
    legacy = {"cn-cli": "models_remote", "intl-cli": "models_intl"}
    return CONFIG.get(legacy[profile]) if profile in legacy else None


def _in_region(profile: str, region: str | None) -> bool:
    return region is None or profile_region(profile) == region


def _configured_profiles(region: str | None) -> set[str]:
    pool = CONFIG.get("cred_pool")
    if pool is not None:
        return {profile for entry in pool.entries() if (profile := pool._entry_profile(entry))
                and model_policy.credential_enabled(CONFIG, entry) and _in_region(profile, region)}
    cm = CONFIG.get("cred")
    if cm is not None:
        profile = cm.summary()["profile"]
        return {profile} if _in_region(profile, region) else set()
    known = {profile for profile in PROFILE_ENDPOINTS
             if _in_region(profile, region) and _catalog_for(profile) is not None}
    return known or ({"intl-cli"} if region == "intl" else {"cn-cli"})


def _usable_models(models):
    return [model for model in models or [] if model.get("id") and model.get("supportsToolCall")
            and not model.get("disabled")]


def _account_scope(account: dict, scope: str = "models") -> list[dict] | None:
    """Merge root candidates into selector models while retaining selector metadata."""
    picker = account.get("models")
    if scope == "models" or picker is None:
        return picker
    seen = {item.get("id") for item in picker}
    # Root entries supply missing names without replacing selector metadata.
    return picker + [item for item in account.get("serves") or [] if item.get("id") not in seen]


def _models_for_profile(profile: str, configured=None, *, scope: str = "models") -> list[dict]:
    models = _catalog_for(profile, scope)
    if models is None:
        # Static fallback is limited to legacy domestic CLI deployments.
        configured = _configured_profiles(profile_region(profile)) if configured is None else configured
        return ([{"id": name, "supportsToolCall": True} for name in DEFAULT_MODELS]
                if CONFIG.get("model_cache") is None and CONFIG.get("account_catalogs") is None
                and profile == "cn-cli" and configured <= {"cn-cli"} else [])
    return _usable_models(models)


def _upstream_model(model: str | None, profile: str) -> str | None:
    return "default-model" if model == "auto" and profile_region(profile) == "intl" else model


def _free_multiplier(credits) -> bool:
    """Check whether the account's catalog explicitly declares a zero credit rate."""
    if not isinstance(credits, str):
        return False
    match = re.fullmatch(r"x\s*0(?:\.0+)?\s*(?:credits?)?", credits.strip(), re.IGNORECASE)
    return match is not None


def _multiplier_value(credits):
    """Parse an official model rate, returning None for missing or unknown formats."""
    if not isinstance(credits, str):
        return None
    match = re.fullmatch(r"x\s*([0-9]+(?:\.[0-9]+)?)\s*(?:credits?)?", credits.strip(), re.IGNORECASE)
    if match is None:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _model_free(models, model: str | None, profile: str) -> bool:
    """Require an explicit zero-rate entry in this account's model catalog."""
    if not model:
        return False
    routed = _upstream_model(model, profile)
    return any(item.get("id") == routed and _free_multiplier(item.get("credits"))
               for item in models or [])


def _model_profiles(model: str | None, region: str | None = None, configured=None) -> set[str]:
    configured = _configured_profiles(region) if configured is None else configured
    profiles = {profile for profile in PROFILE_ENDPOINTS if _in_region(profile, region)}
    if not model:
        return profiles
    supported = {profile for profile in profiles
                 if any(item["id"] == _upstream_model(model, profile)
                        for item in _models_for_profile(profile, configured, scope="serves"))}
    if model == "auto" and region == "cn":
        # WorkBuddy uses its advertised Auto; legacy CLI defaults remain separate.
        if "cn-work" in configured and "cn-work" in supported:
            return {"cn-work"}
        if "cn-cli" in configured and _models_for_profile("cn-cli", configured):
            return {"cn-cli"}
    if model == "auto" and region is None and "cn-cli" in configured and _models_for_profile("cn-cli", configured):
        supported.add("cn-cli")
    if model != "auto" and not supported and not CONFIG.get("model_guard") and len(configured) == 1:
        return configured
    return supported


def _catalog_pending(region: str | None = None) -> bool:
    pool = CONFIG.get("cred_pool")
    if CONFIG.get("model_cache") is None and CONFIG.get("account_catalogs") is None:
        return False
    if pool is not None and pool.sync_pending(region):
        return True
    return not any(_catalog_for(profile) is not None for profile in _configured_profiles(region))


def _profile_has_credits(profile: str) -> bool:
    pool = CONFIG.get("cred_pool")
    if pool is None:
        if profile_region(profile) == "cn":
            return True
        ledger = CONFIG.get("ledger")
        return bool(ledger and any((entry.get("credits") or {}).get("intl")
                                   and float((entry.get("credits") or {}).get("credits") or 0) > 0
                                   for entry in ledger.snapshot().values()))
    return any(pool._entry_profile(entry) == profile and pool._has_credit(entry, profile)
               for entry in pool.entries() if model_policy.credential_enabled(CONFIG, entry))


def current_models(region: str | None = None) -> list[str]:
    """Merge models eligible for current accounts without region-specific client URLs."""
    pool = CONFIG.get("cred_pool")
    if pool is not None:
        pool._rescan()
    with pool._lock if pool is not None else nullcontext():
        configured = _configured_profiles(region)
        out, has_auto = [], False
        auto_profiles = _model_profiles("auto", region, configured)
        if pool is not None and (CONFIG.get("account_catalogs") is not None or CONFIG.get("model_cache") is not None):
            accounts = CONFIG.get("account_catalogs") or {}
            for entry in pool.entries():
                if not model_policy.credential_enabled(CONFIG, entry):
                    continue
                profile = entry.get("profile")
                if not profile or not _in_region(profile, region):
                    continue
                # Publish only advertised zero-rate models for empty accounts.
                zero = pool._zero_balance(entry, profile)
                if not zero and not pool._has_credit(entry, profile):
                    continue
                account = accounts.get(entry.get("account_key")) or {}
                if account.get("profile") != profile:
                    continue
                models = _usable_models(_account_scope(account, "serves"))
                if zero:
                    models = [model for model in models if _free_multiplier(model.get("credits"))]
                out.extend(model["id"] for model in models)
                if profile in auto_profiles and models:
                    has_auto |= profile == "cn-cli" or any(model["id"] == _upstream_model("auto", profile) for model in models)
        else:
            for profile in sorted(configured):
                entries = ([entry for entry in pool.entries() if pool._entry_profile(entry) == profile]
                           if pool is not None else [])
                # Empty products may publish only their advertised zero-rate models.
                zero_only = bool(entries) and all(pool._zero_balance(entry, profile) for entry in entries)
                if _profile_has_credits(profile) or zero_only:
                    models = _models_for_profile(profile, configured)
                    if zero_only:
                        models = [model for model in models if _free_multiplier(model.get("credits"))]
                    out.extend(model["id"] for model in models)
                    has_auto |= bool(models) and profile in auto_profiles
        if has_auto:
            out.append("auto")
        return list(dict.fromkeys(out))


def current_model_details(region: str | None = None) -> list[dict]:
    """Return public model rates with per-profile values and the minimum eligible rate."""
    pool = CONFIG.get("cred_pool")
    if pool is not None:
        pool._rescan()
    details: dict[str, dict] = {}
    for name in current_models(region):
        details[name] = {"id": name, "credits": None, "credits_by_profile": {}}
    if pool is None:
        return list(details.values())
    with pool._lock:
        def record(profile: str, item: dict, *, zero: bool) -> None:
            name = item.get("id")
            if name not in details:
                return
            if zero and not _free_multiplier(item.get("credits")):
                return  # Empty accounts cannot supply paid model rates.
            value = _multiplier_value(item.get("credits"))
            if value is None:
                return
            details[name]["credits_by_profile"][profile] = value
            best = details[name]["credits"]
            details[name]["credits"] = value if best is None else min(best, value)

        if CONFIG.get("account_catalogs") is not None or CONFIG.get("model_cache") is not None:
            accounts = CONFIG.get("account_catalogs") or {}
            for entry in pool.entries():
                if not model_policy.credential_enabled(CONFIG, entry):
                    continue
                profile = entry.get("profile")
                if not profile or not _in_region(profile, region):
                    continue
                zero = pool._zero_balance(entry, profile)
                if not zero and not pool._has_credit(entry, profile):
                    continue
                account = accounts.get(entry.get("account_key")) or {}
                if account.get("profile") != profile:
                    continue
                for item in _usable_models(_account_scope(account, "serves")):
                    record(profile, item, zero=zero)
        else:
            configured = _configured_profiles(region)
            for profile in sorted(configured):
                entries = [entry for entry in pool.entries() if pool._entry_profile(entry) == profile]
                zero_only = bool(entries) and all(pool._zero_balance(entry, profile) for entry in entries)
                if not (_profile_has_credits(profile) or zero_only):
                    continue
                for item in _models_for_profile(profile, configured):
                    record(profile, item, zero=zero_only)
    return list(details.values())


def _client_wants_stream(payload: dict) -> bool:
    """Default stream to false and reject non-Boolean values."""
    value = payload.get("stream", False)
    if not isinstance(value, bool):
        raise HTTPException(status_code=400, detail={"error": {
            "message": "stream must be a boolean", "type": "invalid_request_error", "param": "stream"}})
    return value


def _prepare_payload(payload, field="messages") -> dict:
    """Apply request-wide image limits before adaptation, logging and credential selection."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail={"error": {
            "message": "请求体必须是 JSON 对象", "type": "invalid_request_error"}})
    try:
        prepared, stats = apply_image_policy(
            payload, field=field, max_images=CONFIG["max_images"], policy=CONFIG["image_policy"])
    except ImageLimitError as error:
        _log(f"[limit] 图片超限，拒绝请求 | count={error.count} | limit={error.limit}")
        raise HTTPException(status_code=413, detail={"error": {
            "message": str(error), "type": "invalid_request_error", "param": field,
            "code": "too_many_images", "image_count": error.count, "max_images": error.limit}}) from None
    if stats["dropped"]:
        _log(f"[limit] 保留最新图片 | count={stats['count']} | retained={stats['retained']} | dropped={stats['dropped']}")
    return prepared


def _normalize_tool_choice(body):
    """Map named tool choice to a single required tool for string-only upstream selection."""
    choice = body.get("tool_choice")
    if not isinstance(choice, dict):
        return
    function = choice.get("function", choice)
    name = function.get("name") if isinstance(function, dict) else None
    tools = body.get("tools")
    matches = [tool for tool in tools if isinstance(tool, dict) and tool.get("type") == "function"
               and isinstance(tool.get("function"), dict) and tool["function"].get("name") == name] if isinstance(tools, list) else []
    if choice.get("type") != "function" or not isinstance(name, str) or not name.strip() or len(matches) != 1:
        raise HTTPException(status_code=400, detail={"error": {"message": "tool_choice must name exactly one declared function",
                            "type": "invalid_request_error", "param": "tool_choice"}})
    body["tools"], body["tool_choice"] = matches, "required"


def _bind_request_session(payload, body):
    context = current_context()
    if context is not None and context.scoped:
        try:
            context.bind_session(payload, body.get("messages"))
        except SessionIdentifierError as error:
            raise HTTPException(status_code=400, detail={"error": {"message": str(error),
                                "type": "invalid_request_error", "param": "session_id"}}) from None
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise HTTPException(status_code=400, detail={"error": {"message": "invalid session input",
                                "type": "invalid_request_error"}}) from None


def _request_id():
    context = current_context()
    return context.request_id if context is not None else uuid.uuid4().hex


def _prepare_chat_body(body: dict, *, region=None, session_payload=None) -> dict:
    """Normalize models, system messages, streaming, desensitization and payload budgets."""
    if session_payload is not None:
        _bind_request_session(session_payload, body)
    body = dict(body)
    body["model"] = model_policy.resolve(CONFIG, body.get("model", "auto"))
    guard_model(body["model"], region=region, resolved=True)
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages or any(not isinstance(message, dict) for message in messages):
        raise HTTPException(status_code=400, detail={"error": {
            "message": "messages must be a non-empty array of objects", "type": "invalid_request_error"}})
    # Upstreams reject developer roles; copy them as system messages without changing content.
    messages = [
        dict(message, role="system") if message.get("role") == "developer" else message
        for message in messages
    ]
    body["messages"] = messages
    if messages[0].get("role") != "system":
        system_index = next((index for index, message in enumerate(messages) if message.get("role") == "system"), None)
        if system_index is None:
            messages = [{"role": "system", "content": "You are a helpful assistant."}, *messages]
        else:
            messages = [messages[system_index], *messages[:system_index], *messages[system_index + 1:]]
        body["messages"] = messages
    _normalize_tool_choice(body)
    body["stream"] = True
    body.setdefault("stream_options", {"include_usage": True})
    body = _chat_body_desensitize(body)
    _guard_request_size(body)
    return body


def _guard_request_size(body: dict) -> int:
    """Validate and measure upstream JSON bytes without truncating text or tool arguments."""
    size = 0
    limit = CONFIG["max_request_bytes"]
    try:
        for part in json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), allow_nan=False).iterencode(body):
            size += len(part.encode("utf-8"))
            if size > limit:
                _log(f"[limit] 请求体超限，拒绝请求 | limit_bytes={limit}")
                raise HTTPException(status_code=413, detail={"error": {
                    "message": f"处理后的请求体超过网关上限 {limit} 字节，请缩短历史或压缩图片",
                    "type": "invalid_request_error", "code": "request_too_large", "max_bytes": limit}})
    except (ValueError, UnicodeError) as error:
        raise HTTPException(status_code=400, detail={"error": {
            "message": "请求体包含无法序列化的 JSON 值", "type": "invalid_request_error"}}) from None
    return size


def guard_model(name: str, *, region=None, resolved=False) -> None:
    """Reject unauthorized models and route only through accounts with confirmed support."""
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(status_code=400, detail={"error": {
            "message": "model must be a non-empty string", "type": "invalid_request_error", "param": "model"}})
    if not resolved:
        name = model_policy.resolve(CONFIG, name)
    model_policy.check_resolved(CONFIG, name)
    if not CONFIG.get("model_guard"):
        return
    pool = CONFIG.get("cred_pool")
    if pool is not None:
        pool._rescan()
    if _model_profiles(name, region):
        return
    if _catalog_pending(region):
        raise HTTPException(status_code=503, headers={"Retry-After": "3"}, detail={"error": {
            "message": "模型目录正在同步，请稍后重试", "type": "service_unavailable", "code": "catalog_syncing"}})
    raise HTTPException(status_code=404, detail={"error": {
        "message": f"The model '{name}' is not supported by this gateway. See GET /v1/models.",
        "type": "invalid_request_error", "param": "model", "code": "model_not_found"}})



@app.get("/v1/models")
def list_models(authorization: Optional[str] = Header(default=None),
                x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_auth(authorization, x_api_key)
    data = [{"id": item["id"], "object": "model", "created": 1700000000, "owned_by": "codebuddy",
             "credits": item["credits"], "credits_by_profile": item["credits_by_profile"]}
            for item in model_policy.public_details(sys.modules[__name__])]
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request,
                           authorization: Optional[str] = Header(default=None),
                           x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_auth(authorization, x_api_key)
    # Select sticky credentials while building upstream headers.

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})

    payload = _prepare_payload(payload)
    # Aggregation supports exactly one completion, so reject other n values.
    n_value = payload.get("n")
    if n_value is not None and not (isinstance(n_value, int) and not isinstance(n_value, bool) and n_value == 1):
        raise HTTPException(status_code=400, detail={"error": {
            "message": "only n=1 is supported: multiple candidates would be merged into one answer",
            "type": "invalid_request_error", "param": "n"}})
    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {"message": "messages is required", "type": "invalid_request_error"}})

    # Forward only supported request fields.
    client_wants_stream = _client_wants_stream(payload)
    body = {k: payload[k] for k in PASSTHROUGH_BODY_KEYS if k in payload}
    body = await run_in_threadpool(_prepare_chat_body, body, session_payload=payload)

    # Record request metadata.
    model_name = payload.get("model", "auto")
    tool_names = [t.get("function", {}).get("name") for t in (payload.get("tools") or [])
                  if isinstance(t, dict)]
    last_user = _last_user_text(messages)
    rid = _request_id()
    _log(f"[{rid}] ▶ REQUEST {model_name} | stream={client_wants_stream} | msgs={len(messages)}"
         + (f" | tools={tool_names}" if tool_names else "")
         + (f" | last_user={_truncate(last_user, 60)!r}" if last_user else ""))
    # Credential selection and refresh perform blocking file and network I/O.
    prepared = body        # Keep canonical input for failover policy checks.
    body, cred, headers, url = await run_in_threadpool(_route_chat, payload, body, rid)
    _log_json(f"[{rid}] REQUEST BODY (发往后端，预览)", body)
    t0 = time.time()

    if client_wants_stream:
        def attempt(routed, cred, headers, url):
            return _stream_upstream(url, headers, routed, model_name, t0, rid, cred=cred)
        return _routed_stream(payload, prepared, model_name, rid, t0, attempt,
                              body, cred, headers, url)

    # Aggregate upstream SSE for non-streaming clients.
    async def fetch(routed, cred, headers, url):
        return await _fetch_checked_chat(url, headers, routed, model_name, rid, cred,
                                         filter_retry=True)
    # Watch for disconnects across the entire failover sequence.
    try:
        collected = await await_or_hangup(
            _routed_fetch(payload, prepared, model_name, rid, t0, fetch,
                          body, cred, headers, url), request)
    except ClientHungUp:
        return _hungup_response(rid, model_name, t0)
    _log_finish(model_name, t0, collected, rid)
    if CONFIG.get("control_store") is not None:
        collected = {**collected, "model": model_name}
    return JSONResponse(content=collected)


def _last_user_text(messages: list) -> str:
    """Extract the latest user text for bounded log previews."""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    return str(blk.get("text", ""))
            return ""
        return str(content)
    return ""


def _log_finish(model_name: str, t0: float, result: dict, rid: str = ""):
    """Log request timing, finish reason, usage, tools and bounded response previews."""
    elapsed = time.time() - t0
    prefix = f"[{rid}] " if rid else ""
    choice = (result.get("choices") or [{}])[0]
    finish = choice.get("finish_reason")
    msg = choice.get("message") or {}
    detector = ContentFilterDetector()
    detector.feed(msg, finish)
    if detector.detected:
        return  # Filtered responses must not expose echoed content in previews.
    tcs = msg.get("tool_calls") or []
    usage = result.get("usage") or {}
    tc_names = [t.get("function", {}).get("name") for t in tcs]
    _log(f"{prefix}◀ RESPONSE {model_name} | {elapsed:.1f}s | finish={finish}"
         + (f" | tool_calls={tc_names}" if tc_names else "")
         + f" | tokens={usage.get('total_tokens', '?')}")
    _log_json(f"{prefix}RESPONSE BODY (预览)", result)


def _chat_completion(merged: dict) -> dict:
    message = {"role": "assistant", "content": merged["content"] or None}
    for key in ("reasoning_content", "refusal", "tool_calls"):
        if merged.get(key):
            message[key] = merged[key]
    return {
        "id": "chatcmpl-" + os.urandom(12).hex(), "object": "chat.completion",
        "created": int(time.time()), "model": merged.get("model") or "unknown",
        "choices": [{"index": 0, "message": message,
                     "finish_reason": merged.get("finish_reason") or ("tool_calls" if merged.get("tool_calls") else "stop")}],
        "usage": merged.get("usage") or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _completion_to_merged(result: dict) -> dict:
    choice = result["choices"][0]
    return {**choice["message"], "finish_reason": choice["finish_reason"],
            "model": result.get("model"), "usage": result.get("usage")}


async def _collect_stream(response: httpx.Response, *, accumulator=None) -> dict:
    """Collect content, reasoning and tools while validating stream completion."""
    accumulator = accumulator if accumulator is not None else ChatSSEAccumulator()
    async for line in response.aiter_lines():
        accumulator.feed_line(line)
        if accumulator.done:
            break
    return _chat_completion(accumulator.result())


_TOOL_CALL_MAX_RETRY = 3


def _tool_choice_satisfied(tool_calls, body):
    choice = body.get("tool_choice")
    if choice == "none":
        return not tool_calls
    if choice != "required":
        return True
    names = {tool.get("function", {}).get("name") for tool in body.get("tools", [])
             if isinstance(tool, dict) and isinstance(tool.get("function"), dict)}
    return bool(tool_calls) and all(call.get("function", {}).get("name") in names for call in tool_calls)


def _tool_calls_healthy(tool_calls, body: dict | None = None) -> bool:
    """Validate tool names and require arguments to encode a JSON object."""
    if not tool_calls:
        return True
    names = {tool.get("function", {}).get("name") for tool in (body or {}).get("tools", [])
             if isinstance(tool, dict) and isinstance(tool.get("function"), dict)} if body is not None else None
    for tc in tool_calls:
        if not isinstance(tc.get("id"), str) or not tc["id"].strip():
            return False
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        if not name.strip() or not (fn.get("arguments") or "").strip():
            return False
        # Valid JSON must still be an object; check names only when tools were declared.
        if names and name not in names:
            return False
        try:
            arguments = json.loads(fn.get("arguments") or "")
        except Exception:
            return False
        if not isinstance(arguments, dict):
            return False
    return True


def _merge_chat_sse_text(text: str) -> dict:
    """Use the shared SSE accumulator for collected text responses."""
    accumulator = ChatSSEAccumulator(max_collect_bytes=CONFIG.get("max_collect_bytes", 0))
    for line in text.splitlines():
        accumulator.feed_line(line)
    return accumulator.result()


def _chat_result_to_sse_lines(m: dict) -> list[str]:
    """Replay collected Chat output as SSE, emitting reasoning before content."""
    content = m.get("content") or ""
    reasoning = m.get("reasoning_content") or ""
    tcs = m.get("tool_calls") or []
    finish = m.get("finish_reason") or "stop"
    model = m.get("model")
    # All chunks in a completion share one stable identifier.
    completion_id = "chatcmpl-" + os.urandom(12).hex()
    created = int(time.time())

    def _line(delta: dict, fr=None) -> str:
        payload = {"id": completion_id, "object": "chat.completion.chunk", "created": created,
                   "choices": [{"index": 0, "delta": delta, "finish_reason": fr}]}
        if model:
            payload["model"] = model
        return "data: " + json.dumps(payload, ensure_ascii=False)

    lines = [_line({"role": "assistant", "content": ""})]
    for i in range(0, len(reasoning), 48):
        lines.append(_line({"reasoning_content": reasoning[i:i + 48]}))
    for i in range(0, len(content), 48):
        lines.append(_line({"content": content[i:i + 48]}))
    refusal = m.get("refusal") or ""
    for i in range(0, len(refusal), 48):
        lines.append(_line({"refusal": refusal[i:i + 48]}))
    for i, tc in enumerate(tcs):
        lines.append(_line({"tool_calls": [dict(tc, index=i)]}))
    lines.append(_line({}, finish))
    if m.get("usage"):
        usage_chunk = {"id": completion_id, "object": "chat.completion.chunk", "created": created,
                       "choices": [], "usage": m["usage"]}
        if model:
            usage_chunk["model"] = model
        lines.append("data: " + json.dumps(usage_chunk, ensure_ascii=False))
    lines.append("data: [DONE]")
    return lines


def _network_error_text(error: Exception) -> str:
    return sanitize_log_text(f"{type(error).__name__}: {str(error).strip() or 'upstream transport failed'}", 512)

def _public_sse_line(line, model_name):
    if CONFIG.get("control_store") is not None and line.startswith("data:"):
        try:
            event = json.loads(line[5:].strip())
            if isinstance(event, dict) and ("model" in event or "choices" in event):
                event["model"] = model_name
                return "data: " + json.dumps(event, ensure_ascii=False)
        except (ValueError, TypeError):
            pass
    return line


@asynccontextmanager
async def _backend_stream(url, headers, body, *, timeout=300, rid="", model_name="?"):
    started, opened = time.monotonic(), False
    context = current_context()
    if context is not None:
        context.attempt = None

    def attempt_headers():
        if context is None:
            return dict(headers)
        attempt = context.start_attempt()
        outgoing = context.attempt_headers(headers, attempt)
        profile = profile_for_headers(headers)
        observe_attempt("upstream_attempt", profile=profile, upstream_model=body.get("model"),
                        credential=account_key(profile, headers.get("X-User-Id"), headers.get("X-Enterprise-Id")),
                        conversation_id=outgoing.get("X-Conversation-ID"),
                        upstream_request_id=outgoing.get("X-Request-ID"))
        return outgoing

    def retry(error):
        """Record connection retries and flag possible billing after write timeouts."""
        timeout_on_write = isinstance(error, WRITE_TIMEOUT_TRANSPORT)
        observe_attempt("write_timeout_retry" if timeout_on_write else "connect_retry",
                        error_code=type(error).__name__,
                        duration_ms=(time.monotonic() - started) * 1000)
        _log(f"[{rid}] {'写超时重放' if timeout_on_write else '建连失败'}，重试 1/1 | {model_name}"
             f" | {_network_error_text(error)}{_replay_cost_note(error)}")
    try:
        resources = request_resources.get()
        clients = resources.clients if resources is not None and CONFIG.get("upstream_keepalive") else None
        async with open_backend_stream(url, headers, body, read_timeout=timeout, on_retry=retry,
                                       retry_write_timeout=bool(CONFIG.get("retry_write_timeout")),
                                       clients=clients, headers_for_attempt=attempt_headers) as response:
            opened = True
            observe_attempt("upstream_http", status_code=response.status_code,
                            duration_ms=(time.monotonic() - started) * 1000)
            yield response
    except (httpx.HTTPError, UpstreamResponseError) as error:
        if not opened:
            observe_attempt("transport", error_code=type(error).__name__,
                            duration_ms=(time.monotonic() - started) * 1000)
        raise


def _safe_err_raw(raw: bytes, status: int) -> dict:
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {"error": {"message": raw.decode("utf-8", "replace")[:500], "type": "upstream_error", "code": status}}


def _check_upstream_status(status, raw, cred, model, *, headers=None):
    if status != 200:
        retry_after = parse_retry_after((headers or {}).get("Retry-After"))
        if not is_filter_error(raw):
            _note_cred_status(cred, status, model=model, raw=raw, retry_after=retry_after)
        raise UpstreamHTTPError(status, raw, retry_after=retry_after)


def _upstream_failure(error, model_name, t0, rid):
    """Normalize failure logs and payloads before endpoint-specific error wrapping."""
    if isinstance(error, UpstreamResponseError):
        status, raw = error.status, error.raw
        category = f"HTTP {status}"
    else:
        status, raw = 502, _network_error_text(error).encode("utf-8")
        category = "网络错误"
    if isinstance(error, UpstreamResponseError) and is_filter_error(raw):
        _note_content_filter(rid, model_name, final=True)
        return status, raw
    else:
        observe_failure(f"upstream_{status}" if isinstance(error, UpstreamResponseError) else type(error).__name__)
    elapsed = time.time() - t0 if t0 else 0
    _log(f"[{rid}] ✗ {category} | {model_name} | {elapsed:.1f}s | {sanitize_log_text(raw.decode('utf-8', 'replace'), 512)}")
    _log_text_body(f"[{rid}] ERROR BODY", raw.decode("utf-8", "replace"))
    return status, raw


def _hungup_response(rid, model_name, t0):
    """Finish a disconnected ASGI request with an empty 204; auditing records cancellation."""
    elapsed = time.time() - t0 if t0 else 0
    _log(f"[{rid}] ✂ 下游已断连，取消这次聚合 | {model_name} | {elapsed:.1f}s")
    return Response(status_code=204)


async def _fetch_checked_chat(url, headers, body, model_name, rid, cred=None, *, filter_retry=False):
    """Collect and validate replies with bounded tool repair and one eligible filter fallback."""
    tool_attempt = 0
    filter_retried = False
    while True:
        accumulator = ChatSSEAccumulator(max_collect_bytes=CONFIG.get("max_collect_bytes", 0))
        rejection = None
        async with _backend_stream(url, headers, body, rid=rid, model_name=model_name) as response:
            if response.status_code != 200:
                _check_upstream_status(response.status_code, await read_bounded_error(response), cred, body.get("model"),
                                       headers=response.headers)
            else:
                _note_cred_model_ok(cred, body.get("model"))
            try:
                result = await _collect_stream(response, accumulator=accumulator)
            except UpstreamResponseError as error:
                if (not accumulator.done or not accumulator.filter_detector.detected
                        or accumulator.saw_output):
                    raise
                rejection = error
                result = None

        detector = accumulator.filter_detector
        if detector.detected:
            retry_body = body
            if (filter_retry and not filter_retried and detector.retry_safe
                    and CONFIG.get("desensitize") and CONFIG.get("no_compact")):
                retry_body = _chat_body_desensitize(body, force_compact=True)
                try:
                    if _guard_request_size(retry_body) >= _guard_request_size(body):
                        retry_body = body
                except HTTPException:
                    retry_body = body
            if retry_body != body:
                _note_content_filter(rid, model_name, final=False)
                body = retry_body
                filter_retried = True
                continue
            if rejection is not None:
                raise rejection
            _note_content_filter(rid, model_name, final=True)

        calls = result["choices"][0]["message"].get("tool_calls")
        if _tool_calls_healthy(calls, body) and (detector.detected or _tool_choice_satisfied(calls, body)):
            observe_usage(result.get("usage") or {})
            return result
        # Content filtering must not trigger tool-repair regeneration.
        budget = CONFIG.get("tool_call_max_retry", _TOOL_CALL_MAX_RETRY)
        if detector.detected or not body.get("tools") or tool_attempt >= budget:
            if not detector.detected and body.get("tools"):
                # Account for the final failed generation before returning an error.
                exhausted = result.get("usage") or {}
                observe_attempt("tool_args_exhausted", attempt=tool_attempt, max_attempts=budget,
                                total_tokens=exhausted.get("total_tokens"))
            raise UpstreamResponseError(502, b"Invalid upstream tool_calls after retries")
        tool_attempt += 1
        # Discarded generations still consume credits and belong in the audit trail.
        discarded = result.get("usage") or {}
        observe_attempt("tool_args_retry", attempt=tool_attempt, max_attempts=budget,
                        total_tokens=discarded.get("total_tokens"))
        _log(f"[{rid}] tool_calls 损坏，重试 {tool_attempt}/{budget} | {model_name}")

async def _chat_sse_lines(url, headers, body, model_name, t0, rid, cred=None, *, aggregate=False):
    """Yield validated Chat SSE with bounded filter detection and no streaming filter retries."""
    if aggregate:
        result = await _fetch_checked_chat(url, headers, body, model_name, rid, cred)
        for line in _chat_result_to_sse_lines(_completion_to_merged(result)):
            yield line
            yield ""
        _log_finish(model_name, t0, result, rid)
        return
    tracker = ChatSSEAccumulator(collect=False)
    preview = bytearray()
    budget = CONFIG["log_body_limit"] if CONFIG.get("log_path") else 0
    async with _backend_stream(url, headers, body, rid=rid, model_name=model_name) as response:
        if response.status_code != 200:
            _check_upstream_status(response.status_code, await read_bounded_error(response), cred, body.get("model"),
                                   headers=response.headers)
        else:
            _note_cred_model_ok(cred, body.get("model"))
        async for line in response.aiter_lines():
            tracker.feed_line(line)
            if tracker.done or tracker.finish_reason:
                tracker.result()  # Validate completion before emitting a success marker.
            remaining = budget - len(preview)
            if remaining > 0:
                preview.extend((line[:remaining] + "\n").encode("utf-8")[:remaining])
            yield line
            if tracker.done:
                yield ""
                break
    merged = tracker.result()
    observe_usage(merged.get("usage") or {})
    if tracker.filter_detector.detected:
        _note_content_filter(rid, model_name, final=True)
        return
    _log(f"[{rid}] ◀ RESPONSE {model_name} | {time.time() - t0:.1f}s | stream finish={merged['finish_reason']}"
         + f" | tokens={(merged['usage'] or {}).get('total_tokens', '?')}")
    _log_text_body(f"[{rid}] RESPONSE SSE PREVIEW", preview.decode("utf-8", "replace"))


async def _stream_upstream(url: str, headers: dict, body: dict,
                           model_name: str = "?", t0: float = 0.0, rid: str = "", cred=None):
    sent = False
    try:
        async for line in _chat_sse_lines(url, headers, body, model_name, t0, rid, cred, aggregate=bool(body.get("tools"))):
            sent = True
            yield (_public_sse_line(line, model_name) + "\n").encode("utf-8")
    except (httpx.HTTPError, UpstreamResponseError) as error:
        if not sent:
            raise      # Preserve the HTTP error while no response bytes have been sent.
        status, raw = _upstream_failure(error, model_name, t0, rid)
        yield _err_event(raw, status)




def _err_event(msg: bytes, status: int) -> bytes:
    chunk = {"error": {"message": sanitize_log_text(msg.decode("utf-8", "replace"), 512),
                       "type": "upstream_error", "code": status}}
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")


def _cred_manager(cred):
    """Unwrap a credential lease, retaining support for a standalone manager."""
    return cred[0] if isinstance(cred, tuple) else cred


# Retryable upstream auth, quota and gateway responses; deterministic request errors are excluded.
FAILOVER_CODES = frozenset({401, 403, 429, 502, 503, 504})
# Connection failures occur before any request body is sent.
REPLAYABLE_TRANSPORT = (httpx.ConnectError, httpx.ConnectTimeout)
# Write-timeout replay requires explicit opt-in because partial requests may already be billed.
WRITE_TIMEOUT_TRANSPORT = (httpx.WriteTimeout,)
# Gateway timeouts may follow billable upstream work and need an explicit cost warning.
POSSIBLY_CHARGED_CODES = frozenset({502, 504})


def _replay_cost_note(error) -> str:
    """Label replays that may duplicate already billed work."""
    if isinstance(error, UpstreamHTTPError) and error.status in POSSIBLY_CHARGED_CODES:
        return " | 上游可能已处理该请求"
    if isinstance(error, WRITE_TIMEOUT_TRANSPORT):
        return " | 上游可能已处理该请求（正文未写完）"
    return ""


def _failover_safe(error, raw=b"") -> bool:
    """Allow configured pre-response transport or HTTP failover, never filter or incomplete-stream replay."""
    if is_filter_error(raw):
        return False
    if isinstance(error, UpstreamHTTPError):
        return error.status in FAILOVER_CODES
    if isinstance(error, UpstreamResponseError):
        return False
    if isinstance(error, WRITE_TIMEOUT_TRANSPORT):
        return bool(CONFIG.get("retry_write_timeout"))
    return isinstance(error, REPLAYABLE_TRANSPORT)


class _StreamFailure(Exception):
    """Carry the HTTP error and original exception from stream preflight."""

    def __init__(self, status, raw, error=None):
        self.status = status
        self.raw = raw
        self.error = error
        self.headers = error.headers if isinstance(error, UpstreamHTTPError) else None
        super().__init__(f"stream failed before first byte (HTTP {status})")


# Bound teardown waits by cycles so repeated cancellation cannot cause a busy loop.
TEARDOWN_GRACE_CYCLES = 100
TEARDOWN_POLL_SECONDS = 0.01


def _drain_teardown(future) -> None:
    """Retrieve teardown exceptions without rethrowing them."""
    if not future.cancelled():
        future.exception()


async def _teardown_finished(task) -> None:
    """Wait briefly for isolated cleanup, then drain it in the background without delaying cancellation."""
    for _ in range(TEARDOWN_GRACE_CYCLES):
        if task.done():
            _drain_teardown(task)
            return
        try:
            await asyncio.wait([task], timeout=TEARDOWN_POLL_SECONDS)
        except asyncio.CancelledError:
            pass
    if not task.done():
        task.add_done_callback(_drain_teardown)


async def _first_segment(agen):
    """Read one stream segment in a shielded task so cancellation cannot interrupt its cleanup."""
    task = asyncio.ensure_future(agen.__anext__())
    try:
        return await asyncio.shield(task)
    except BaseException:
        task.cancel()
        await _teardown_finished(task)
        raise


async def _stream_segments(agen):
    """Read cancellable segments while allowing the generator's cleanup to finish."""
    while True:
        try:
            yield await _first_segment(agen)
        except StopAsyncIteration:
            return


async def _preflight_stream(agen, model_name, t0, rid):
    """Read the first segment before committing HTTP 200, preserving pre-response error status."""
    try:
        return await _first_segment(agen)
    except StopAsyncIteration:
        empty = UpstreamResponseError(502, b'{"error":{"message":"upstream returned an empty stream",'
                                      b'"type":"upstream_error","code":"empty_response"}}')
        status, raw = _upstream_failure(empty, model_name, t0, rid)
        raise _StreamFailure(status, raw, empty) from None
    except (httpx.HTTPError, UpstreamResponseError) as error:
        status, raw = _upstream_failure(error, model_name, t0, rid)
        raise _StreamFailure(status, raw, error) from None


async def _close_stream(agen) -> None:
    """Close the upstream generator in an isolated task that survives repeated cancellation."""
    if agen is None:
        return

    async def close() -> None:
        try:
            await agen.aclose()
        except Exception:
            pass

    await _teardown_finished(asyncio.ensure_future(close()))


def _chunk_bytes(chunk, charset: str = "utf-8"):
    return chunk if isinstance(chunk, (bytes, memoryview)) else chunk.encode(charset)


class _DeferredStreamResponse(StreamingResponse):
    """Run stream preflight and failover inside ASGI disconnect monitoring before sending headers."""

    def __init__(self, plan):
        self._plan = plan        # Async callable returning the upstream iterator and first segment.
        super().__init__(content=(), media_type="text/event-stream",
                         headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def stream_response(self, send) -> None:
        agen, first = await self._plan()
        try:
            await send({"type": "http.response.start", "status": self.status_code,
                        "headers": self.raw_headers})
            await send({"type": "http.response.body", "body": _chunk_bytes(first, self.charset),
                        "more_body": True})
            async for chunk in _stream_segments(agen):
                await send({"type": "http.response.body", "body": _chunk_bytes(chunk, self.charset),
                            "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            await _close_stream(agen)


def _failover_limit() -> int:
    return int(CONFIG.get("failover_max") or 0)


async def _stream_plan(payload, canonical, model_name, rid, t0, make, routed, cred, headers, url):
    """Prefetch with bounded credential failover, using canonical input to preserve routing restrictions."""
    tried = []
    recovered = None
    while True:
        stream = make(routed, cred, headers, url)
        try:
            first = await _preflight_stream(stream, model_name, t0, rid)
        except _StreamFailure as failure:
            await _close_stream(stream)   # Release the failed upstream connection.
            release_credential(cred)
            recovered = observe_failure_seq()   # Recover only this failure sequence.
            tried.append(cred)
            limit = _failover_limit()
            surface = HTTPException(status_code=failure.status, headers=failure.headers,
                                    detail=_safe_err_raw(failure.raw, failure.status))
            if limit <= 0 or len(tried) > limit or not _failover_safe(failure.error, failure.raw):
                raise surface from None
            try:
                attempt = await run_in_threadpool(_route_chat, payload, canonical, rid,
                                                  tried={_cred_manager(item) for item in tried})
            except HTTPException:
                raise surface from None      # Preserve the failure when no alternative account exists.
            if _cred_manager(attempt[1]) in {_cred_manager(item) for item in tried}:
                raise surface from None
            routed, cred, headers, url = attempt
            _log(f"[{rid}] ↻ 换凭证重放 {len(tried)}/{limit} | {model_name} | 上游 HTTP "
                 f"{failure.status} → {profile_for_headers(headers)}"
                 f"{_replay_cost_note(failure.error)}")
            continue                       # Prefetch from the replacement credential.
        except BaseException:
            # Release the current upstream before propagating cancellation or unexpected errors.
            await _close_stream(stream)
            raise
        if tried:
            # Preserve newer failures such as content filtering on the replacement account.
            observe_recovery(recovered)
        return stream, first


def _routed_stream(payload, canonical, model_name, rid, t0, make, routed, cred, headers, url):
    """Defer stream preflight and failover until ASGI disconnect monitoring is active."""
    return _DeferredStreamResponse(
        lambda: _stream_plan(payload, canonical, model_name, rid, t0, make,
                             routed, cred, headers, url))


async def _routed_fetch(payload, canonical, model_name, rid, t0, fetch, routed, cred, headers, url):
    """Apply bounded non-streaming failover while preserving canonical routing restrictions."""
    tried = []
    recovered = None
    while True:
        try:
            collected = await fetch(routed, cred, headers, url)
            if tried:
                # Recover the earlier attempt without erasing a newer failure.
                observe_recovery(recovered)
            return collected
        except (httpx.HTTPError, UpstreamResponseError) as error:
            release_credential(cred)
            status, raw = _upstream_failure(error, model_name, t0, rid)
            recovered = observe_failure_seq()
            tried.append(cred)
            limit = _failover_limit()
            surface = HTTPException(status_code=status, detail=_safe_err_raw(raw, status),
                                    headers=error.headers if isinstance(error, UpstreamHTTPError) else None)
            if limit <= 0 or len(tried) > limit or not _failover_safe(error, raw):
                raise surface from None
            try:
                attempt = await run_in_threadpool(_route_chat, payload, canonical, rid,
                                                  tried={_cred_manager(item) for item in tried})
            except HTTPException:
                raise surface from None
            if _cred_manager(attempt[1]) in {_cred_manager(item) for item in tried}:
                raise surface from None
            routed, cred, headers, url = attempt
            _log(f"[{rid}] ↻ 换凭证重放 {len(tried)}/{limit} | {model_name} | 上游 HTTP "
                 f"{status} → {profile_for_headers(headers)}"
                 f"{_replay_cost_note(error)}")


def _note_content_filter(rid, model_name, *, final):
    stage = "content_filter" if final else "content_filter_retry"
    observe_attempt(stage, error_code="content_filter")
    if final:
        observe_failure("content_filter")
    action = "保留上游拒绝，不切换账号" if final else "纯审核拒绝，压缩模板重试 1/1"
    _log(f"[{rid}] 内容审核拦截 | {model_name} | {action}")


def _chat_body_desensitize(body: dict, *, force_compact: bool = False) -> dict:
    if not CONFIG.get("desensitize"):
        return body
    return desensitize_body(
        body,
        roles=("system", "developer"),
        desensitize_harness_user=True,
        desensitize_tools=True,
        compact_harness=(force_compact or not CONFIG.get("no_compact")),
        strip_tool_metadata=not CONFIG.get("keep_tool_metadata", False),
    )


# ---------------------------------------------------------------------------
# OpenAI Responses endpoint
# ---------------------------------------------------------------------------

@app.post("/v1/responses")
async def create_response(request: Request,
                          authorization: Optional[str] = Header(default=None),
                          x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Serve Responses requests through the shared Chat upstream and event adapter."""
    _check_auth(authorization, x_api_key)

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})

    payload = _prepare_payload(payload, field="input")
    # Reject server-side conversation references because this gateway is stateless.
    for stateful in ("previous_response_id", "conversation"):
        if payload.get(stateful):
            raise HTTPException(status_code=400, detail={"error": {
                "message": f"{stateful} is not supported: this gateway keeps no server-side response state; resubmit the full input instead",
                "type": "invalid_request_error", "param": stateful}})
    # Convert Responses input to Chat format.
    try:
        chat_body = responses_request_to_chat(payload)
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"request conversion error: {e}", "type": "invalid_request_error"}})

    await run_in_threadpool(_bind_request_session, payload, chat_body)
    chat_body, projection_stats = project_responses_chat_body(
        chat_body, keep_tool_metadata=CONFIG.get("keep_tool_metadata", False))
    chat_body = await run_in_threadpool(_prepare_chat_body, chat_body)

    client_wants_stream = _client_wants_stream(payload)
    model_name = payload.get("model", "auto")
    rid = _request_id()
    _log(f"[{rid}] ▶ RESPONSES {model_name} | stream={client_wants_stream} | input_items={len(payload.get('input', []))}")
    _log(
        f"[{rid}] ── RESPONSES PROJECTION ── "
        f"mode={projection_stats.get('mode')} "
        f"| msgs {projection_stats.get('original_messages')}→{projection_stats.get('projected_messages')} "
        f"| chars {projection_stats.get('original_message_chars')}→{projection_stats.get('projected_message_chars')} "
        f"| tools {projection_stats.get('original_tools')}→{projection_stats.get('projected_tools')} "
        f"| tool_chars {projection_stats.get('original_tool_chars')}→{projection_stats.get('projected_tool_chars')} "
        f"| summarized_history={projection_stats.get('summarized_history_messages', 0)} "
        f"| dropped_harness={projection_stats.get('dropped_harness_messages', 0)} "
        f"| anchor_user={projection_stats.get('anchor_user_preserved', False)}"
    )
    # Keep blocking credential selection and refresh off the event loop.
    prepared = chat_body        # Preserve canonical input for routing policy checks.
    chat_body, cred, headers, url = await run_in_threadpool(_route_chat, payload, chat_body, rid)
    _log_json(f"[{rid}] RESPONSES → CHAT BODY (预览)", chat_body)
    t0 = time.time()

    if client_wants_stream:
        def attempt(routed, cred, headers, url):
            return _stream_responses(url, headers, routed, model_name, t0, rid, cred=cred)
        return _routed_stream(payload, prepared, model_name, rid, t0, attempt,
                              chat_body, cred, headers, url)

    return await _nonstream_adapted(url, headers, chat_body, model_name, t0, rid, cred,
                                    payload=payload, canonical=prepared, request=request)


async def _nonstream_adapted(url, headers, body, model_name, t0, rid, cred, *, anthropic=False,
                             payload=None, canonical=None, request=None):
    converter = (AnthropicStreamConverter(model=model_name) if anthropic else ResponsesStreamConverter(model=model_name, parallel_tool_calls=body.get("parallel_tool_calls", True)))

    async def fetch(routed, cred, headers, url):
        return await _fetch_checked_chat(url, headers, routed, model_name, rid, cred,
                                         filter_retry=True)
    try:
        collected = await await_or_hangup(
            _routed_fetch(payload, body if canonical is None else canonical,
                          model_name, rid, t0, fetch, body, cred, headers, url), request)
        for line in _chat_result_to_sse_lines(_completion_to_merged(collected)):
            converter.feed_line(_public_sse_line(line, model_name))
        converter.finish()
    except (httpx.HTTPError, UpstreamResponseError) as error:
        status, raw = _upstream_failure(error, model_name, t0, rid)
        raise HTTPException(status_code=status, detail=_safe_err_raw(raw, status),
                            headers=error.headers if isinstance(error, UpstreamHTTPError) else None) from None
    except ClientHungUp:
        return _hungup_response(rid, model_name, t0)
    result = converter.get_nonstream_response()
    _log_finish(model_name, t0, collected, rid)
    return JSONResponse(content=result)


async def _stream_adapted(url, headers, body, model_name, t0, rid, cred=None, *, anthropic=False):
    """Map protocol events while sharing connection, aggregation and failure handling."""
    converter = (AnthropicStreamConverter(model=model_name) if anthropic else ResponsesStreamConverter(model=model_name, parallel_tool_calls=body.get("parallel_tool_calls", True)))
    sent = False
    try:
        async for line in _chat_sse_lines(
                url, headers, body, model_name, t0, rid, cred,
                aggregate=not anthropic or bool(body.get("tools"))):
            events = converter.feed_line(_public_sse_line(line, model_name))
            if events:
                sent = True
                yield events.encode("utf-8")
        events = converter.finish()
        if events:
            sent = True
            yield events.encode("utf-8")
    except (httpx.HTTPError, UpstreamResponseError) as error:
        if not sent:
            raise      # Preserve the HTTP error before any response bytes are sent.
        status, raw = _upstream_failure(error, model_name, t0, rid)
        event = {"type": "error", "error": {
            "message": sanitize_log_text(raw.decode("utf-8", "replace"), 512),
            "type": "api_error" if anthropic else "upstream_error", "code": status}}
        prefix = "event: error\n" if anthropic else ""
        yield (prefix + f"data: {json.dumps(event, ensure_ascii=False)}\n\n").encode("utf-8")


async def _stream_responses(url: str, headers: dict, body: dict,
                            model_name: str = "?", t0: float = 0.0, rid: str = "", cred=None):
    async for chunk in _stream_adapted(url, headers, body, model_name, t0, rid, cred):
        yield chunk


# ---------------------------------------------------------------------------
# Anthropic Messages endpoint
# ---------------------------------------------------------------------------

@app.post("/v1/messages")
async def create_message(request: Request,
                         authorization: Optional[str] = Header(default=None),
                         x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Serve Anthropic Messages through the shared Chat upstream and event adapter."""
    _check_auth(authorization, x_api_key)

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})

    payload = _prepare_payload(payload)
    # Convert Anthropic messages and tools to Chat format.
    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {"message": "messages is required", "type": "invalid_request_error"}})

    try:
        chat_body = anthropic_request_to_chat(payload)
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"request conversion error: {e}", "type": "invalid_request_error"}})

    chat_body = await run_in_threadpool(_prepare_chat_body, chat_body, session_payload=payload)
    model_name = payload.get("model", "auto")
    chat_messages = chat_body.get("messages", [])
    rid = _request_id()
    _log(f"[{rid}] ▶ ANTHROPIC {model_name} | msgs={len(chat_messages)} | anthropic_msgs={len(messages)}")
    # Keep blocking credential selection and refresh off the event loop.
    prepared = chat_body        # Preserve canonical input for routing policy checks.
    chat_body, cred, headers, url = await run_in_threadpool(_route_chat, payload, chat_body, rid)
    _log_json(f"[{rid}] ANTHROPIC → CHAT BODY (预览)", chat_body)
    t0 = time.time()

    if not _client_wants_stream(payload):
        return await _nonstream_adapted(url, headers, chat_body, model_name, t0, rid, cred,
                                        anthropic=True, payload=payload, canonical=prepared,
                                        request=request)

    def attempt(routed, cred, headers, url):
        return _stream_anthropic(url, headers, routed, model_name, t0, rid, cred=cred)
    return _routed_stream(payload, prepared, model_name, rid, t0, attempt,
                          chat_body, cred, headers, url)


async def _stream_anthropic(url: str, headers: dict, body: dict,
                            model_name: str = "?", t0: float = 0.0, rid: str = "", cred=None):
    async for chunk in _stream_adapted(url, headers, body, model_name, t0, rid, cred, anthropic=True):
        yield chunk


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request,
                       authorization: Optional[str] = Header(default=None),
                       x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Return heuristic token estimates for Anthropic request budgeting."""
    _check_auth(authorization, x_api_key)
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail={"error": {"message": "请求体必须是 JSON 对象", "type": "invalid_request_error"}})
    return {"input_tokens": _estimate_input_tokens(payload)}


def _estimate_input_tokens(payload: dict) -> int:
    """Estimate tokens from character counts and message overhead, not upstream billing."""

    def measure(value) -> int:
        if isinstance(value, str):
            ascii_chars = sum(1 for ch in value if ord(ch) < 128)
            return (ascii_chars + 3) // 4 + (len(value) - ascii_chars)
        if isinstance(value, list):
            return sum(measure(item) for item in value)
        if isinstance(value, dict):
            return sum(measure(item) for item in value.values())
        return 0

    total = measure(payload.get("system")) + measure(payload.get("tools"))
    messages = payload.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict):
                total += measure(message.get("content")) + 4  # Message structure overhead.
    return total


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def preflight() -> bool:
    files = find_auth_files()
    sys.stderr.write("==== 预检 ====\n")
    sys.stderr.write(f"平台      : {sys.platform}\n")
    sys.stderr.write(f"Python    : {sys.version.split()[0]}\n")
    sys.stderr.write(f"后端      : {BACKEND} (直连，原生 function calling)\n")
    sys.stderr.write(f"自管目录  : {managed_auth_dir()}\n")
    sys.stderr.write(f"登录文件  : {len(files)} 个\n")
    if not os.environ.get("CODEBUDDY_AUTH_DIR"):
        sys.stderr.write(f"种子来源  : {', '.join(str(d) for d in auth_dirs())}\n")
    ok = True
    if not files:
        sys.stderr.write("\n[警告] 未找到登录文件。请运行 python3 converter.py login 扫码添加账号，或用 --auth-file 指定。\n")
        ok = False
    for af in files:
        try:
            cm = CredentialManager(af)
            info = cm.summary()
            sys.stderr.write(f"账号      : {info.get('nickname')} / {info.get('enterpriseName')}  ({af.name})\n")
            sys.stderr.write(f"token过期 : {'是(将自动刷新)' if info['token_expired'] else '否'}\n")
        except Exception as e:
            sys.stderr.write(f"[警告] 读取凭据失败 {af.name}：{e}\n")
            ok = False
    sys.stderr.write("================\n")
    return ok


def login(site: str = "cn", open_browser: bool = True) -> int:
    """Complete browser login and persist managed credentials without local HTTP calls."""
    import webbrowser

    try:
        started = _OAUTH.start(site=site)
        uri = started["verification_uri"]
        print(f"请打开以下链接扫码登录：\n{uri}", flush=True)
        if open_browser:
            try:
                opened = webbrowser.open(uri)
            except webbrowser.Error:
                opened = False
            if not opened:
                print("无法自动打开浏览器，请手动打开上面的链接。", flush=True)
        print("正在等待扫码授权；网页显示登录成功后，请继续等待终端确认入库。\n"
              "按 Ctrl+C 取消。", flush=True)
        while True:
            result = _OAUTH.poll(started["login_id"])
            if result.get("done"):
                if result.get("error") or not result.get("cred"):
                    print(f"登录失败：{result.get('error') or '未获取到凭据'}", file=sys.stderr)
                    return 1
                target = _save_oauth_credential(result["cred"])
                print(f"登录成功，账号已保存至：{target}\n"
                      "使用同一凭据目录的服务会在下次请求时自动加载（默认目录扫描模式）。",
                      flush=True)
                return 0
            time.sleep(1.5)
    except KeyboardInterrupt:
        print("\n已取消登录。", file=sys.stderr)
        return 130
    except CredentialFileError as e:
        print(f"登录失败：{e}", file=sys.stderr)
        return 1
    except OSError:
        print("登录失败：无法保存凭据，请检查凭据目录的写入权限。", file=sys.stderr)
        return 1
    except (httpx.HTTPError, ValueError, RuntimeError):
        # Upstream errors may contain authorization URLs or sensitive response data.
        print("登录失败：登录接口请求失败或响应无效，请检查网络后重试。", file=sys.stderr)
        return 1


def _nonnegative_int(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("必须为非负整数")
    return number


def _positive_int(value):
    number = _nonnegative_int(value)
    if number == 0:
        raise argparse.ArgumentTypeError("必须为正整数")
    return number


def _boolean_arg(value):
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in ("true", "1", "yes", "on"):
        return True
    if normalized in ("false", "0", "no", "off"):
        return False
    raise argparse.ArgumentTypeError("必须为 true 或 false")


def main():
    ap = argparse.ArgumentParser(description="CodeBuddy -> OpenAI 兼容转换器（直连后端）")
    ap.add_argument("command", nargs="?", choices=("serve", "login"), default="serve",
                    help="serve 启动服务（默认）；login 扫码登录、自动轮询并保存账号")
    ap.add_argument("--site", choices=tuple(auth_oauth.SITE_HOSTS), default="cn",
                    help="login 站点：cn 国内站（默认）；intl 国际 WorkBuddy；intl-codebuddy 国际 CodeBuddy")
    ap.add_argument("--no-browser", action="store_true",
                    help="login 仅显示授权链接，不自动打开浏览器（服务器/容器环境）")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址；覆盖 CODEBUDDY2API_BIND")
    ap.add_argument("--port", type=int, default=8787, help="监听端口；覆盖 CODEBUDDY2API_PORT")
    ap.add_argument("--api-key", default=os.environ.get("CODEBUDDY2API_KEY", ""),
                    help="可选：要求客户端携带的 API key（默认不校验）")
    ap.add_argument("--admin-csrf", type=_boolean_arg, nargs="?", const=True,
                    default=os.environ.get("CODEBUDDY2API_ADMIN_CSRF", "true"),
                    help="管理 Origin/CSRF 校验，默认 true；仅在受信任本地环境设为 false，鉴权仍启用")
    ap.add_argument("--log", default=None, metavar="PATH",
                    help="额外写入兼容文本日志（如 --log converter.log 或 --log /tmp/cb.log）。"
                         "不传仍记录 SQLite 审计，但不输出文本文件。")
    ap.add_argument("--desensitize", action="store_true",
                    help="适配固定 CLI 模板、压缩运行时提示并零宽脱敏关键词。默认关闭。")
    ap.add_argument("--no-compact", action="store_true",
                    help="配合 --desensitize 保留主要行为指令，仍适配固定模板并裁剪运行时元数据；"
                         "非流式纯审核拒绝最多压缩兜底一次。")
    ap.add_argument("--keep-tool-metadata", type=_boolean_arg, nargs="?", const=True,
                    default=os.environ.get("CODEBUDDY2API_KEEP_TOOL_METADATA", "false"),
                    help="保留工具描述及参数 description/title；启用脱敏时仍处理描述文本，默认 false")
    ap.add_argument("--skip-check", action="store_true", help="跳过启动预检")
    ap.add_argument("--auth-file", action="append", default=[], metavar="PATH",
                    help="凭据文件（可重复传入组成凭证池；默认自动扫描 auth 目录全部 *.info）")
    ap.add_argument("--credit-price-cny", type=float, default=None, metavar="PRICE",
                    help="积分折算单价（元/Credit），默认 0.014（旗舰版连续包月 700元/5万积分摊算）")
    ap.add_argument("--usd-rate", type=float, default=None, metavar="RATE",
                    help="人民币→美元汇率，影响 /v1/dashboard/billing 端点金额")
    ap.add_argument("--credit-price-usd", type=float, default=None, metavar="PRICE",
                    help="国际站积分折算单价（美元/Credit），默认 0.03（Pro 加量包 $15/500 积分）")
    ap.add_argument("--model-catalog-ttl", type=int, default=6 * 3600, metavar="SECONDS",
                    help="云端模型表缓存有效期，默认 21600 秒（6 小时）；TTL 内不再打 /v3/config")
    ap.add_argument("--no-model-guard", action="store_true",
                    help="关闭表外模型本地拦截；默认拦截，避免无效请求打到上游并触发扣费")
    ap.add_argument("--max-images", type=_nonnegative_int, metavar="N",
                    default=os.environ.get("CODEBUDDY2API_MAX_IMAGES", "16"),
                    help="单请求图片上限，默认 16；0 表示不允许图片")
    ap.add_argument("--image-policy", choices=("truncate", "error"),
                    default=os.environ.get("CODEBUDDY2API_IMAGE_POLICY", "truncate"),
                    help="超额图片策略：truncate 保留最新图片（默认），error 返回 413")
    ap.add_argument("--max-request-bytes", type=_positive_int, metavar="BYTES",
                    default=os.environ.get("CODEBUDDY2API_MAX_REQUEST_BYTES", str(32 * 1024 * 1024)),
                    help="图片处理与适配后请求体的字节上限，默认 32 MiB")
    ap.add_argument("--max-inbound-bytes", type=_positive_int, metavar="BYTES",
                    default=os.environ.get("CODEBUDDY2API_MAX_INBOUND_BYTES", str(64 * 1024 * 1024)),
                    help="入站原始请求体字节上限（解析前生效，含 chunked），默认 64 MiB")
    ap.add_argument("--max-collect-bytes", type=_nonnegative_int, metavar="BYTES",
                    default=os.environ.get("CODEBUDDY2API_MAX_COLLECT_BYTES", str(8 * 1024 * 1024)),
                    help="聚合路径输出收集总字节上限（正文+思考+工具参数），默认 8 MiB；0 不限制")
    ap.add_argument("--max-concurrent", type=_nonnegative_int, metavar="N",
                    default=os.environ.get("CODEBUDDY2API_MAX_CONCURRENT", "64"),
                    help="推理端点并发上限（超出立即 503），默认 64；0 不限制")
    ap.add_argument("--max-inflight-per-account", type=_nonnegative_int, metavar="N",
                    default=os.environ.get("CODEBUDDY2API_MAX_INFLIGHT_PER_ACCOUNT", "0"),
                    help="单账号在途上限，默认 0（不限制）；满载返回 503，不借容量切换到收费账号")
    ap.add_argument("--upstream-keepalive", type=_boolean_arg, nargs="?", const=True,
                    default=os.environ.get("CODEBUDDY2API_UPSTREAM_KEEPALIVE", "false"),
                    help="按上游入口复用有界连接池，默认 false；重启生效，不改变超时或重放规则")
    ap.add_argument("--request-context-mode", choices=("legacy", "scoped"),
                    default=os.environ.get("CODEBUDDY2API_REQUEST_CONTEXT_MODE", "legacy"),
                    help="请求上下文：legacy 保持旧会话头，scoped 启用显式会话与逐尝试追踪；默认 legacy")
    ap.add_argument("--log-body-limit", type=_nonnegative_int, metavar="BYTES",
                    default=os.environ.get("CODEBUDDY2API_LOG_BODY_LIMIT", "65536"),
                    help="每条正文日志的预览字节上限，默认 64 KiB；0 只记录摘要")
    ap.add_argument("--tool-call-max-retry", type=_nonnegative_int, metavar="N",
                    default=os.environ.get("CODEBUDDY2API_TOOL_CALL_MAX_RETRY", "3"),
                    help="工具参数损坏时的额外生成上限，默认 3；0 表示不重试（每次额外生成都消耗额度）")
    ap.add_argument("--failover-max", type=_nonnegative_int, metavar="N",
                    default=os.environ.get("CODEBUDDY2API_FAILOVER_MAX", "0"),
                    help="失败发生在向下游落第一个字节之前时，最多换几个凭证就地重放，默认 0（关闭）；"
                         "只重放上游没收下请求体或用 401/403/429/502/503/504 拒绝的失败；"
                         "写请求体超时需另开 --retry-write-timeout 才参与")
    ap.add_argument("--retry-write-timeout", type=_boolean_arg, nargs="?", const=True,
                    default=os.environ.get("CODEBUDDY2API_RETRY_WRITE_TIMEOUT", "false"),
                    help="把「写请求体超时」也算作上游没收下请求体从而参与重放，默认 false。写超时只能"
                         "证明正文没写完，上游是否已按半截正文计费看不到，因此要显式开启（同时作用于连接"
                         "重试与 --failover-max 换凭证重放）")
    ap.add_argument("--auto-trial", type=_boolean_arg, nargs="?", const=True,
                    default=None, help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.auto_trial is not None or "CODEBUDDY2API_AUTO_TRIAL" in os.environ:
        sys.stderr.write("[trial] AUTO_TRIAL / --auto-trial 已停用；请在 WebUI 凭证页手动领取体验积分。\n")
    del args.auto_trial
    if args.image_policy not in ("truncate", "error"):
        ap.error("CODEBUDDY2API_IMAGE_POLICY 必须为 truncate 或 error")
    if args.command == "login":
        return login(site=args.site, open_browser=not args.no_browser)

    for key in ("max_images", "image_policy", "max_request_bytes", "log_body_limit",
                "tool_call_max_retry", "max_inbound_bytes", "max_collect_bytes", "max_concurrent",
                "failover_max", "retry_write_timeout", "upstream_keepalive", "max_inflight_per_account",
                "request_context_mode"):
        CONFIG[key] = getattr(args, key)
    CONFIG["api_key"] = args.api_key
    CONFIG["desensitize"] = args.desensitize
    CONFIG["no_compact"] = args.no_compact
    CONFIG["credit_price_cny"] = args.credit_price_cny or None
    CONFIG["usd_rate"] = args.usd_rate or None
    CONFIG["credit_price_usd"] = args.credit_price_usd or None
    CONFIG["model_guard"] = not args.no_model_guard
    # File logging is enabled only when a path is configured.
    CONFIG["log_path"] = args.log if args.log else os.environ.get("CODEBUDDY2API_LOG")
    from app import runtime_management
    runtime_management.initialize(sys.modules[__name__], args, parser=ap)
    # Validate effective binding and authentication before credential scans or background work.
    if (args.host not in ("127.0.0.1", "::1", "localhost") and not CONFIG.get("api_key")
            and os.environ.get("CODEBUDDY2API_ALLOW_OPEN_NOAUTH", "").lower() not in ("1", "true", "yes")):
        runtime_management.close(CONFIG)
        ap.error("非回环绑定且未设置 API key 会匿名开放推理额度；"
                 "请设置 CODEBUDDY2API_KEY，或确知风险后以 CODEBUDDY2API_ALLOW_OPEN_NOAUTH=true 显式放行")
    files = [Path(p) for p in args.auth_file]
    if not files:
        seed_credentials()  # Seed missing desktop credentials into managed storage.
    CONFIG["cred_pool"] = CredentialPool(files, scan=not files,
                                         blocks_path=managed_auth_dir() / "model-site-blocks.json")
    CONFIG["cred"] = CONFIG["cred_pool"].first()
    CONFIG["account_catalogs"] = {}  # Disable static fallback before maintenance starts.
    if credits_mod is not None:
        ledger = credits_mod.CreditLedger(managed_auth_dir() / "credits-ledger.json")
        CONFIG["ledger"] = ledger
        CONFIG["model_cache"] = credits_mod.ModelCatalogCache(
            managed_auth_dir() / "model-catalog.json", ttl=args.model_catalog_ttl)
        CONFIG["cred_pool"].set_ledger(ledger)  # Verify balance ownership before publishing catalogs.
    _publish_model_cache()
    runtime_management.install(sys.modules[__name__])
    threading.Thread(target=_refresher_loop, args=(CONFIG["cred_pool"],),
                     daemon=True, name="cred-refresher").start()
    if credits_mod is not None:
        threading.Thread(target=_housekeeper_loop, args=(CONFIG["cred_pool"], ledger),
                         daemon=True, name="cred-housekeeper").start()

    if not args.skip_check:
        preflight()

    sys.stderr.write(f"\n✅ 监听 http://{args.host}:{args.port}（直连后端，原生 function calling）\n")
    sys.stderr.write(f"   WebUI     : http://{args.host}:{args.port}/dashboard\n")
    sys.stderr.write("   SQLite 审计默认开启，凭证仍以 .info 文件保存\n")
    sys.stderr.write("   GET  /v1/models\n")
    sys.stderr.write("   POST /v1/chat/completions   (原生 tools/tool_calls，支持流式)\n")
    sys.stderr.write("   POST /v1/responses          (Responses API，Codex CLI 兼容)\n")
    sys.stderr.write("   POST /v1/messages           (Anthropic API，Claude Code / CC Switch 兼容)\n")
    sys.stderr.write("   GET  /health\n")
    sys.stderr.write("   GET/POST/DELETE /admin/credentials  (凭证池管理)\n")
    sys.stderr.write("   添加账号：python3 converter.py login（自动等待扫码并保存）\n")
    if credits_mod is not None:
        sys.stderr.write("   GET  /admin/credits           (积分/签到状态)\n")
        sys.stderr.write("   POST /admin/checkin           (仅签到，按日幂等)\n")
        sys.stderr.write("   POST /admin/sync              (同步余额、目录与用量，不签到)\n")
        sys.stderr.write("   每日签到 + 快过期积分优先调度已启用\n")
    if args.api_key:
        sys.stderr.write("   鉴权已启用（API key 已设置）\n")
    if not CONFIG["admin_csrf"]:
        sys.stderr.write("   警告：管理 Origin/CSRF 校验已关闭，仅限受信任本地环境；API key 与会话校验仍启用。\n")
    sys.stderr.write(f"   图片限制  : {CONFIG['max_images']} 张/请求，策略 {CONFIG['image_policy']}\n")
    if CONFIG["log_path"]:
        sys.stderr.write(f"   日志      : {CONFIG['log_path']}\n")
    if args.desensitize:
        mode = "零宽脱敏 + 保留全文" if args.no_compact else "零宽脱敏 + 压缩摘要"
        sys.stderr.write(f"   脱敏      : 已启用（{mode}）\n")
    sys.stderr.write("按 Ctrl+C 退出。\n\n")

    # Record service startup.
    _log(f"==== converter 启动 ====")

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        runtime_management.close(CONFIG)


if __name__ == "__main__":
    sys.exit(main())
