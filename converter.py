#!/usr/bin/env python3
"""Expose CodeBuddy and WorkBuddy through compatible Chat, Responses and Messages APIs."""

from __future__ import annotations
import base64

import argparse
import asyncio
import weakref
import hashlib
import itertools
import json
import math
import os
import random
import re
import secrets
import shutil
import sys
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler as _default_http_exception_handler
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
import uvicorn as uvicorn  # Keep the existing embedding/test hook.

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
from app.adapters.responses_projection import PROJECTION_MODES, project_responses_chat_body
from app.adapters.anthropic_adapter import (
    anthropic_request_to_chat,
    AnthropicStreamConverter,
)

from app import auth_oauth
from app.settings import SCHEMA
from app import trial_rewards
from app import buddy, daily_chat, growth_chains, growth_tasks, checkin as checkin_service, model_policy, travel
from app.credential_cooldowns import CredentialCooldowns
from app.model_blocks import ModelBlocks
from app.usage_snapshots import UsageSnapshots
from app.client_hangup import ClientHungUp, await_or_hangup
from app.observability import (AuditMiddleware, observe_recovery, observe_route, observe_stream_mode,
                               observe_responses_projection, observe_usage, observe_attempt,
                               observe_failure, observe_failure_seq, observe_discard,
                               observe_nested)
from app.credential_io import (CredentialFileError, read_import_file, atomic_write_credential,
                               credential_file_lock)
from app.upstream_io import (ChatSSEAccumulator, StreamOutputBudget, UpstreamHTTPError,
                             UpstreamResponseError, open_backend_stream, parse_retry_after,
                             read_bounded_error)
from app.inference_resources import (AccountCapacity, InferenceResourcesMiddleware, inference_lifespan,
                                     request_resources, release_credential)
from app.request_context import SessionIdentifierError, current_context
from app.reasoning import resolve_reasoning_effort, thinking_mode
from app import model_capabilities
from app.message_normalization import merge_intl_user_images
from app.adapters.chat_input import normalize_chat_messages
from app.model_catalog_view import INTERNATIONAL as SHARED_INTL_PROFILES, share_models
from app.catalog_index import CatalogIndex
from app.routing_scheduler import RoutingScheduler
from app.catalog_probe import AggregateCatalog
from app.catalog_index import account_scope as _catalog_account_scope
from app import model_rates
from app.rate_measurement import RateBook
from app.inference_auth import require_api_key
from app.admin_auth import SessionStoreError
from app.content_filter import ContentFilterDetector, is_filter_error
from app.upstream_compat import (channel_fallback_url, is_channel_check_error, normalize_tool_schemas,
                                 should_channel_fallback)
from app.request_limits import ImageLimitError, apply_image_policy
from app.safe_logging import format_log_body, sanitize_log_text
from app import perf_metrics
from app.startup import allows_open_noauth, capture_launch, load_startup_env, restart_process, run_server
from app.site_routing import (DOMESTIC, INTERNATIONAL, PROFILE_ENDPOINTS, site_for_auth, site_for_headers,
                              profile_for_auth, profile_for_headers, profile_region, profile_product,
                              profile_site, chat_url_for_headers, search_url_for_headers, fetch_url_for_headers,
                              refresh_url_for_auth, media_url_for_headers)
from app import dsml, mcp_server, server_tools, web_fetch, web_search, media_generation, media_store
from app.inbound_limits import anthropic_path
from app.client_profiles import (CLI_VERSION, CLI_USER_AGENT, credential_headers, catalog_cache_key,
                                 account_key, canonical_uid, enterprise_id_of, identity_email,
                                 is_enterprise_account, normalize_uid)
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

_STREAM_MODES = ("compatible", "realtime")
_REQUEST_POLICY_KEY = object()  # Process-local object key; never serializable or client injectable.


@dataclass(frozen=True)
class _StreamRequestPolicy:
    mode: str
    aggregate: bool
    max_collect_bytes: int

    @property
    def realtime(self):
        return self.mode == "realtime"


def _snapshot_stream_policy(protocol: str, body: dict) -> _StreamRequestPolicy:
    """Freeze hot streaming choices and memory policy for one request and all failovers."""
    mode = CONFIG.get("stream_mode", "compatible")
    if mode not in _STREAM_MODES:
        raise ValueError("invalid stream mode")
    compatible_aggregate = protocol == "responses" or bool(body.get("tools"))
    return _StreamRequestPolicy(mode, mode == "compatible" and compatible_aggregate,
                                max(0, int(CONFIG.get("max_collect_bytes", 0) or 0)))


def _body_with_stream_policy(body: dict, policy: _StreamRequestPolicy) -> dict:
    """Attach a process-local snapshot without exposing an upstream payload override."""
    routed = dict(body)
    routed[_REQUEST_POLICY_KEY] = policy
    return routed


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
    """Read the canonical account identity for deduplication, or return None."""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return _credential_account(d).get("uid")
    except Exception:
        return None

def _credential_account(data: dict) -> dict:
    """Return the account block, preferring a populated ``account`` object.

    An empty ``account`` object must not shadow a populated ``accounts[0]``: some
    exports carry both, and treating ``{}`` as authoritative makes the credential
    look like it has no uid at all.
    """
    account = data.get("account")
    if isinstance(account, dict) and account:
        return account
    accounts = data.get("accounts")
    candidate = accounts[0] if isinstance(accounts, list) and accounts and isinstance(accounts[0], dict) else None
    if candidate:
        return candidate
    return account if isinstance(account, dict) else {}


def _credential_identity(data: dict) -> str:
    """Hash the canonical account identity, falling back to a real email without a uid.

    The uid is normalised through the shared helper so a numeric uid hashes exactly
    like its string form and is never mistaken for a missing one.
    """
    account = _credential_account(data)
    return account_key(profile_for_auth(data.get("auth") or {}), canonical_uid(account),
                       enterprise_id_of(account))


def _enterprise_dispatch_id(token: str, account: dict, *, uid: str = "", domain: str = "") -> str:
    """Return the tenant id that selects the enterprise billing endpoint.

    A stored tenant id is used directly. A credential that is enterprise by type
    alone (historical backups) needs one lookup; if that lookup cannot confirm the
    tenant the sync must fail with a readable error instead of quietly querying the
    personal endpoint, which answers with an empty package list for enterprise
    accounts and would surface as "no credits available".
    """
    enterprise_id = enterprise_id_of(account)
    if enterprise_id is not None:
        return enterprise_id
    if not is_enterprise_account(account):
        return ""
    resolved = credits_mod.resolve_enterprise_id(token, uid=uid, domain=domain)
    if not resolved:
        raise RuntimeError("企业账号缺少企业 ID，无法查询企业配额（已跳过个人版接口以避免误报余额）")
    return resolved


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
        perf_metrics.inc("credential_stat_calls")
        st = self.path.stat()
        return st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size

    def _file_changed(self) -> bool:
        """Whether the on-disk file moved since the last load; one stat, no re-read."""
        try:
            return self._cached is None or self._file_version() != self._mtime
        except OSError:
            return True

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
        s = self._cached
        auth = s.get("auth")
        if isinstance(auth, dict):
            # Encrypted envelope tokens must fail fast instead of leaking into upstream headers.
            auth_oauth.ensure_plaintext_tokens(auth)
        return s

    def _is_expired(self, s: dict | None = None) -> bool:
        if s is None:
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
            code, msg, body = _diagnostic_from_payload(data)
            raise TokenRefreshRejected(f"刷新 token 失败：{data.get('msg', data)}",
                                       code=code, msg=msg, body=body)
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
            # 复用同一次会话读取判断过期，稳态只做一次文件版本检查；刷新后才重读。
            s = self._session()
            if self._is_expired(s):
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


    def token_age_s(self) -> float | None:
        """Seconds since the stored token was last refreshed; None when unknown."""
        with self._lock:
            auth = self._session().get("auth") or {}
            last = auth.get("lastRefreshTime")
            return None if not last else max(0.0, time.time() - last / 1000)

    def summary(self) -> dict:
        perf_metrics.inc("credential_summary_calls")
        with self._lock:
            s = self._session()
            auth = s.get("auth") or {}
            acct = _credential_account(s)
            profile = profile_for_auth(auth)
            # ``uid`` here is the canonical identity value (a real email when no uid
            # exists), identical to the input of ``account_key`` below, so callers that
            # gate on it treat a uid-less account exactly like the pool does.
            identity_uid = canonical_uid(acct)
            return {
                "uid": identity_uid or None,
                "account_key": _credential_identity(s),
                "site": profile_site(profile),
                "profile": profile, "region": profile_region(profile), "product": profile_product(profile),
                "nickname": acct.get("nickname"),
                "enterpriseName": acct.get("enterpriseName"),
                "enterprise": is_enterprise_account(acct),
                "token_expires_at": auth.get("expiresAt", 0),
                "token_expired": self._is_expired(s),
                "last_refresh_time": auth.get("lastRefreshTime") or 0,
            }


STORAGE_WARN_INTERVAL = 300  # Rate limit for persistence-failure warnings
USAGE_CACHE_MAX_AGE_S = 7 * 24 * 3600   # A cached usage snapshot older than this is not shown.
STICKY_TTL = 30 * 60        # Idle session binding lifetime in seconds
STICKY_MAX = 512            # Session binding capacity
CRED_COOLDOWN = 300         # Credential cooldown in seconds
MODEL_COOLDOWN = 600        # Model cooldown when a 429 omits reset time
MODEL_COOLDOWN_MAX = 86400  # Maximum model cooldown in seconds
UPSTREAM_BODY_AUDIT_LIMIT = 256 * 1024  # Bounded upstream error JSON kept per failed attempt.
RESTRICTED_CODES = frozenset({"11140"})  # Upstream account-restriction codes; disable, never requeue.
FRESH_401_LIMIT = 3         # Fresh-token 401s inside the window before auto-disable
FRESH_401_WINDOW = 3600     # Sliding window for fresh-token 401 counting
REFRESH_REJECT_LIMIT = 3    # Consecutive business refresh rejections before auto-disable
REFRESH_REJECT_WINDOW = 3600  # Rejections further apart than this are not consecutive
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


def _diagnostic_from_payload(payload):
    """Extract (code, msg, bounded body) from an upstream JSON payload, for audit only."""
    if not isinstance(payload, dict):
        return None, None, None
    # The nested error envelope carries the business verdict; a generic outer
    # code (e.g. "error") must not mask a terminal nested code such as 11140.
    inner = payload.get("error")
    nodes = [inner] if isinstance(inner, dict) else []
    nodes.append(payload)
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
    return (code[:64] or None), (msg[:256] or None), (format_log_body(payload, UPSTREAM_BODY_AUDIT_LIMIT) or None)


def _upstream_diagnostic(raw: bytes):
    """Extract the business code, message and bounded JSON body from an upstream error."""
    if not raw:
        return None, None, None
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, UnicodeError, RecursionError):
        return None, None, None
    return _diagnostic_from_payload(payload)


class TokenRefreshRejected(RuntimeError):
    """The upstream refresh endpoint answered with a business rejection, not a network fault."""

    def __init__(self, message, *, code=None, msg=None, body=None):
        super().__init__(message)
        self.code, self.msg, self.body = code, msg, body


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


# Globally unique revisions keep cached policy views and model tables from matching a rebuilt pool.
_POOL_REVISION = itertools.count(1)


class CredentialPool:
    """Manage credential discovery, reloads, sticky sessions, cooldowns and refresh."""

    def __init__(self, paths: list[Path] | None = None, scan: bool = False,
                 blocks_path: Path | None = None, cooldowns_path: Path | None = None, *, state_store=None):
        self._lock = threading.RLock()
        self._entries: list[dict] = []   # {id, cm, fail_until}
        self._sticky: "OrderedDict[str, tuple[str, float]]" = OrderedDict()
        self._model_fail: dict[tuple[str, str], float] = {}  # Per-credential/model 429 expiry
        # Keep unsupported-model backoff isolated by backend and model.
        self._blocks = ModelBlocks(blocks_path, ttl_s=MODEL_SITE_BLOCK_S, max_ttl_s=MODEL_SITE_BLOCK_MAX_S, store=state_store)
        # Cooldowns outlive a restart so a backend that just refused is not retried immediately.
        self._cooldowns = CredentialCooldowns(cooldowns_path, store=state_store)
        self._storage_warned = 0.0   # Rate limit for persistence-failure warnings
        self._rr = {None: 0, "cn": 0, "intl": 0}
        self._ledger = None              # Prefer credits expiring sooner.
        self._capacity = AccountCapacity()
        self._scan = scan                # Scan the auth directory during reconcile, never per request.
        # Request-side lookups are rebuilt whenever the entry list changes.
        self._index_id: dict[str, dict] = {}
        self._index_cm: dict = {}
        self._index_identity: dict[str, list[dict]] = {}
        self.revision = next(_POOL_REVISION)   # Bumped whenever the entry list is rebuilt.
        self._scheduler = RoutingScheduler()   # Revision-keyed eligibility shards.
        # Explicit paths must survive rejection or eviction so a repaired file re-enters.
        self._configured: list[str] = []
        self._ignored_duplicates: set[str] = set()
        self._ignored_invalid: dict[str, str | None] = {}
        self._sync_pending: set[str] = set()
        self._syncing: set[str] = set()
        self._sync_event = threading.Event()
        self._sync_retry: dict[str, float] = {}
        self._sync_attempts: dict[str, int] = {}
        self._index_signature: tuple | None = None   # Semantic entry shape; unchanged reloads keep shards.
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
                if cid not in self._configured:
                    self._configured.append(cid)
                if not os.path.exists(cid):
                    continue
                entry = by_id.get(cid)
                if entry is not None:
                    if (not reset and entry["generation"] == entry["cm"]._generation
                            and not entry["cm"]._file_changed()):
                        # Reconcile re-lists every file; only a moved or re-read file pays
                        # the summary, and a generation gap means the identity may have moved.
                        continue
                    if reset:
                        entry["cm"].invalidate()
                    try:
                        summary = entry["cm"].summary()
                    except auth_oauth.AuthTokenTypeError:
                        # An envelope replacement must not keep serving the stale identity.
                        by_id.pop(cid, None)
                        if entry.get("uid"):
                            have_uids.pop(entry.get("account_key"), None)
                        self._drop_entry(entry)
                        continue
                    except Exception:
                        continue  # A damaged file must not block other credentials.
                    generation = entry["cm"]._generation
                    identity = summary["account_key"]
                    changed = reset or generation != entry.get("generation")
                    if changed:
                        old_identity = entry.get("account_key")
                        old_profile = entry.get("profile")
                        replaced = old_identity != identity
                        if replaced:
                            self._model_fail = {key: until for key, until in self._model_fail.items() if key[0] != cid}
                            self._sticky = OrderedDict((key, value) for key, value in self._sticky.items() if value[0] != cid)
                            # The path now belongs to another account; its cooldowns must not carry over.
                            self.forget_credential_state(entry)
                        if entry.get("uid"):
                            have_uids.pop(old_identity, None)
                        # A same-account background token refresh does not change catalog
                        # entitlements, but it must also not clear a pending refresh marker
                        # set by an import or a representative probe; only _store_catalog does.
                        entry.update(uid=summary.get("uid"), profile=summary["profile"], site=summary["site"],
                                     enterprise=summary.get("enterprise") is True,
                                     account_key=identity, generation=generation,
                                     catalog_dirty=bool(entry.get("catalog_dirty") or reset or replaced
                                                       or old_profile != summary["profile"]))
                        self._bind_entry(entry)
                        if reset or replaced:
                            entry.update(fail_until=0.0, keepalive_after=0.0)
                        if reset and not replaced:
                            # An explicit reload re-evaluates auth for the same account, so the
                            # persisted breaker is lifted; a replacement must keep the incoming
                            # account's own breaker, which is hydrated just below.
                            self._forget_credential_cooldown(entry)
                        self._adopt_cooldowns(entry)
                        self._adopt_auto_state(entry)
                        if entry.get("uid"):
                            have_uids[identity] = cid
                        self._queue_sync(cid)
                    continue
                manager = CredentialManager(Path(cid))
                try:
                    summary = manager.summary()
                except auth_oauth.AuthTokenTypeError as error:
                    # Envelope tokens cannot be refreshed or used; keep such files out of the pool.
                    try:
                        digest = hashlib.sha256(Path(cid).read_bytes()).hexdigest()
                    except OSError:
                        digest = None
                    if self._ignored_invalid.get(cid) != digest:
                        # Suppression is content-keyed so a different invalid credential
                        # reusing the path warns again even without a scan while missing.
                        _log(f"[cred] 拒绝入池（入库校验失败：{error}）: {Path(cid).name}")
                        self._ignored_invalid[cid] = digest
                    continue
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
                         "enterprise": summary.get("enterprise") is True,
                         "account_key": identity_key, "catalog_dirty": True}
                self._bind_entry(entry)
                self._adopt_cooldowns(entry)
                self._adopt_auto_state(entry)
                self._entries.append(entry)
                by_id[cid] = entry
                self._ignored_duplicates.discard(cid)
                self._ignored_invalid.pop(cid, None)
                if uid:
                    have_uids[identity_key] = cid
                self._queue_sync(cid)
            self._rebuild_index()

    def _rebuild_index(self):
        """Rebuild request-side lookups after the entry list changed; never called per request."""
        index_id = {entry["id"]: entry for entry in self._entries}
        index_cm = {entry["cm"]: entry for entry in self._entries}
        identity: dict[str, list[dict]] = {}
        for entry in self._entries:
            if entry.get("account_key"):
                identity.setdefault(entry["account_key"], []).append(entry)
        # Token refreshes rewrite files without changing who is routable, so only a real
        # identity/profile/order move invalidates the revision and the scheduler shards.
        signature = tuple((entry["id"], entry.get("account_key"), entry.get("profile"))
                          for entry in self._entries)
        if signature != self._index_signature:
            self._index_signature = signature
            self.revision = next(_POOL_REVISION)
            self._scheduler.clear()
        self._index_id, self._index_cm, self._index_identity = index_id, index_cm, identity

    def entry_for(self, cm):
        """Return the live entry owning this manager, or None after eviction."""
        return self._index_cm.get(cm)

    def entry_by_id(self, cid):
        """Return the live entry for one credential path."""
        return self._index_id.get(cid)

    def entries_by_identity(self, account_key):
        """Return every live entry sharing one account identity."""
        return tuple(self._index_identity.get(account_key, ()))

    def _queue_sync(self, cid):
        entry = self._index_id.get(cid)
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
            if all_entries:
                # A cycle right after an event must not repeat accounts that just succeeded.
                cutoff = time.time() - HOUSEKEEP_MIN_SYNC_GAP_S
                ids = {entry["id"] for entry in self._entries
                       if entry["id"] in active and float(entry.get("synced_at") or 0.0) <= cutoff}
            else:
                ids = set(self._sync_pending)
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
                entry = self._index_id.get(cid)
                if cid in failed and cid in present and cid not in self._sync_pending:
                    attempt = min(self._sync_attempts.get(cid, 0) + 1, 5)
                    self._sync_attempts[cid] = attempt
                    delay = min(60 * 2 ** (attempt - 1), 900)
                    # Jitter keeps repeated upstream refusals from retrying in lockstep.
                    self._sync_retry[cid] = time.monotonic() + delay * random.uniform(1.0, 1.25)
                else:
                    self._sync_retry.pop(cid, None)
                    self._sync_attempts.pop(cid, None)
                    if entry is not None:
                        entry["synced_at"] = time.time()

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
            entry = self._index_cm.get(cm)
            if entry is None or not model_policy.credential_enabled(CONFIG, entry):
                return False
            if not self._lease_matches(cm, generation):
                self._queue_sync(entry["id"])
                return False
            self.reload([cm.path], reset=False)
            update()
            return True


    def _drop_entry(self, entry):
        """Purge one entry whose file became unusable, mirroring prune()'s cleanup."""
        cid = entry["id"]
        if self._ledger is not None:
            self._ledger.remove(cid)
        self.forget_credential_state(entry)
        self._entries = [e for e in self._entries if e is not entry]
        self._model_fail = {key: until for key, until in self._model_fail.items() if key[0] != cid}
        self._sticky = OrderedDict((key, value) for key, value in self._sticky.items() if value[0] != cid)
        self._sync_pending.discard(cid)
        self._syncing.discard(cid)
        self._sync_retry.pop(cid, None)
        self._sync_attempts.pop(cid, None)
        self._rebuild_index()
        invalidate_model_table()
    def prune(self):
        """Remove missing credential files and their session bindings."""
        with self._lock:
            self._ignored_duplicates = {p for p in self._ignored_duplicates if os.path.exists(p)}
            # A path whose invalid file vanished must warn again when a new file reuses it.
            self._ignored_invalid = {p: v for p, v in self._ignored_invalid.items() if os.path.exists(p)}
            before = len(self._entries)
            removed = [e for e in self._entries if not os.path.exists(e["id"])]
            for entry in removed:
                if self._ledger is not None:
                    self._ledger.remove(entry["id"])
                self.forget_credential_state(entry)
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
            if len(self._entries) != before:
                self._rebuild_index()


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

    def _adopt_cooldowns(self, entry):
        """Hydrate persisted cooldowns once per identity, so the in-memory table stays authoritative."""
        identity, profile = entry.get("account_key"), entry.get("profile")
        if not self._durable_identity(entry):
            return          # Adopt later, once this account's identity is validated.
        if entry.get("cooldowns_adopted") == identity:
            return
        entry["cooldowns_adopted"] = identity
        state = self._cooldowns.restore(identity, profile)
        if not state:
            return
        # Deadlines are absolute and already bounded, so adopting one never extends a cooldown.
        if state.get("fail_until"):
            entry["fail_until"] = max(entry.get("fail_until") or 0.0, state["fail_until"])
            if state.get("reason"):
                entry["last_error"] = state["reason"]
            if state.get("failed_at"):
                entry["last_failure_at"] = state["failed_at"]
        for model, until in (state.get("models") or {}).items():
            key = (entry["id"], model)
            self._model_fail[key] = max(self._model_fail.get(key, 0.0), until)


    def _adopt_auto_state(self, entry):
        """Restore the persisted auto-disable provenance so a restart keeps the label."""
        store = CONFIG.get("control_store")
        if store is None or not entry.get("account_key"):
            return
        try:
            record = store.snapshot()["credentials"].get(entry["account_key"]) or {}
        except Exception:
            return
        if record.get("auto_disabled") is not True or record.get("enabled", True):
            return
        entry["auto_disabled"] = True
        if record.get("auto_reason"):
            entry["last_error"] = record["auto_reason"]
        if record.get("auto_body"):
            entry["last_error_body"] = record["auto_body"][:8192]
        if record.get("auto_at"):
            entry["last_failure_at"] = record["auto_at"]

    def _durable_identity(self, entry) -> bool:
        """Whether this account's identity is complete enough to key durable state.

        A hash is derived from the profile and UID, so an account with no UID still yields a
        stable-looking hash shared by every other account in the same state. Durable rows
        must therefore be keyed only when the identifying components are actually present.
        """
        return bool(entry.get("account_key") and entry.get("profile") and entry.get("uid"))

    def _remember_credential(self, entry, reason):
        """Mirror a credential circuit breaker to disk; returns whether it is durable."""
        if not self._durable_identity(entry):
            return False
        durable = self._cooldowns.note_credential(entry["account_key"], entry["profile"],
                                                 entry["fail_until"], reason=reason)
        self._warn_storage("cooldown", durable)
        return durable

    def _remember_model(self, entry, model, until):
        """Mirror a model cooldown to disk; returns whether it is durable."""
        if not self._durable_identity(entry):
            return False
        durable = self._cooldowns.note_model(entry["account_key"], entry["profile"], model, until)
        self._warn_storage("cooldown", durable)
        return durable

    def _warn_storage(self, label, durable):
        """Report a persistence failure operationally, rate limited so a hot path cannot flood."""
        if durable:
            self._storage_warned = 0.0
            return
        now = time.time()
        if now - getattr(self, "_storage_warned", 0.0) < STORAGE_WARN_INTERVAL:
            return
        self._storage_warned = now
        _log(f"[cred] {label}持久化失败（{self._cooldowns.last_error}）；本次运行仍按内存态生效")

    def _forget_credential_cooldown(self, entry):
        """Lift a persisted breaker while keeping this account's model cooldowns."""
        if self._durable_identity(entry):
            outcome = self._cooldowns.clear_credential(entry["account_key"], entry["profile"])
            self._warn_storage("cooldown", outcome["durable"])

    def cooldown_detail(self) -> list:
        """Return persisted cooldown rows for diagnostics."""
        return self._cooldowns.detail()

    def cooldown_storage(self) -> dict:
        """Report whether cooldown persistence is currently usable."""
        return {"available": self._cooldowns.path is not None, "path": self._cooldowns.path,
                "degraded": self._cooldowns.last_error is not None,
                "last_error": self._cooldowns.last_error, "rows": len(self._cooldowns.detail()),
                "warning": "冷却持久化写入失败，本次运行仍按内存态生效。" if self._cooldowns.last_error else None}

    def clear_cooldowns(self, cm, model: str | None = None) -> dict:
        """Lift a circuit breaker or model cooldown after a confirmed recovery or admin reset.

        Persisting cooldowns removes the old "restart the gateway to clear it" workaround,
        so an explicit reset has to be able to lift one both in memory and on disk. The two
        outcomes are reported separately: an in-memory reset that could not be written is
        not a durable reset, and saying otherwise would hide a cooldown that comes back.
        """
        with self._lock:
            entry = next((e for e in self._entries if e["cm"] is cm), None)
            if entry is None:
                return {"changed_in_memory": False, "durable": False}
            # The in-memory reset always happens, even when this account's identity is not
            # complete enough to key durable state.
            durable = not self._durable_identity(entry)
            if model:
                routed_model = _upstream_model(model, self._entry_profile(entry))
                changed = self._model_fail.pop((entry["id"], routed_model), None) is not None
                if not durable:
                    outcome = self._cooldowns.clear_model(entry["account_key"], entry["profile"], routed_model)
                    durable = outcome["durable"]
                self._warn_storage("cooldown", durable)
                return {"changed_in_memory": changed, "durable": durable}
            changed = entry["fail_until"] > time.time()
            entry["fail_until"] = 0.0
            entry["last_error"] = None
            if not durable:
                durable = self._cooldowns.clear_credential(entry["account_key"], entry["profile"])["durable"]
            self._warn_storage("cooldown", durable)
            return {"changed_in_memory": changed, "durable": durable}

    def reset_cooldowns_for(self, identity: str) -> dict:
        """Lift every cooldown held by one account, addressed by its public identity.

        Persisting cooldowns removed the old "restart the gateway to clear it" workaround, so an
        operator needs a supported way back when a breaker or a 429 cooldown was recorded in
        error, or when upstream has demonstrably recovered. The whole account is reset rather
        than just its circuit breaker: a credential that is mid-429 is exactly the case an
        operator is trying to unstick, and lifting only the breaker would leave it unusable.

        This is a local state change. It never refreshes a token, contacts upstream, or queues
        synchronization, so a subsequent genuine failure is free to arm the cooldown again.
        """
        with self._lock:
            entry = next((e for e in self._entries if e.get("account_key") == identity), None)
            if entry is None:
                raise KeyError(identity)
            now = time.time()
            changed = entry["fail_until"] > now
            entry["fail_until"] = 0.0
            entry["fail_until"] = 0.0
            entry["last_error"] = None
            entry["last_failure_at"] = None
            entry["last_error_body"] = None
            entry["auto_disabled"] = None
            entry["fresh_401s"] = []
            entry["refresh_rejects"] = 0
            for key in [k for k in self._model_fail if k[0] == entry["id"]]:
                if self._model_fail[key] > now:
                    changed = True
                del self._model_fail[key]
            # One durable write for the whole account, replacing any breaker and model rows. An
            # account whose identity is incomplete still resets in memory, but owns no disk row.
            durable = not self._durable_identity(entry)
            if not durable:
                durable = self._cooldowns.forget(entry["account_key"])["durable"]
            self._warn_storage("cooldown", durable)
            return {"changed_in_memory": changed, "durable": durable}

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
        # Explicit mode retries configured paths, including rejected or evicted ones.
        paths = find_auth_files() if self._scan else [Path(cid) for cid in self._configured]
        self.reload(paths, reset=False)

    def fingerprint(self):
        """Return a cheap directory fingerprint so reconcile only runs after a real change."""
        configured = {str(path) for path in find_auth_files()} if self._scan else set(self._configured)
        rows = []
        for cid in sorted(configured):
            try:
                st = os.stat(cid)
            except OSError:
                rows.append((cid, None))
                continue
            rows.append((cid, st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size))
        return (self._scan, tuple(rows))

    def reconcile(self, previous=None):
        """Apply file changes outside the request path; returns the new fingerprint."""
        current = self.fingerprint()
        if previous is None or current != previous:
            self._rescan()
        return current

    def _healthy(self, e: dict, policy=None) -> bool:
        return model_policy.credential_enabled(CONFIG, e, policy) and time.time() >= e["fail_until"]

    @staticmethod
    def _entry_profile(entry):
        # Reload caches the parsed profile, so the request path never re-reads the file here.
        profile = entry.get("profile")
        if profile:
            return profile
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
            # An unlimited enterprise tenant holds no numeric balance and is never zero.
            if balance.get("unlimited") and balance.get("enterprise"):
                return False
            return (bool(balance.get("intl")) == (profile_region(profile) == "intl")
                    and float(balance.get("credits") or 0) <= 0)
        except (TypeError, ValueError):
            return False

    def _has_credit(self, entry, profile):
        balance = (self._ledger.entry(entry["id"]).get("credits") or {}) if self._ledger else {}
        if not balance:
            return profile_region(profile) == "cn"
        try:
            # An unlimited enterprise tenant holds no numeric balance; it has credit on its own site.
            if balance.get("unlimited") and balance.get("enterprise"):
                return bool(balance.get("intl")) == (profile_region(profile) == "intl")
            return (bool(balance.get("intl")) == (profile_region(profile) == "intl")
                    and float(balance.get("credits") or 0) > 0)
        except (TypeError, ValueError):
            return False

    def _eligible(self, entry, model, *, region=None, profile=None, rule=None, policy=None):
        if not model_policy.route_allowed(CONFIG, entry, model, rule=rule, policy=policy):
            return False
        actual = self._entry_profile(entry)
        profile = profile or actual
        if not profile or profile != actual or not _in_region(profile, region):
            return False
        configured = _configured_profiles(region, policy)
        if profile not in _model_profiles(model, region, configured, policy=policy):
            return False
        if CONFIG.get("account_catalogs") is not None or CONFIG.get("model_cache") is not None:
            account = (CONFIG.get("account_catalogs") or {}).get(entry.get("account_key")) or {}
            models = _effective_account_scope(account, "serves", model_id=_upstream_model(model, profile))
            if account.get("profile") != profile or models is None:
                return False
            usable = _usable_models(models)
            supported = any(item["id"] == _upstream_model(model, profile) for item in usable)
            cli_auto = model == "auto" and profile == "cn-cli" and bool(usable)
            # Disabling the guard must not borrow another account's model capabilities.
            declared = any(item["id"] == _upstream_model(model, profile)
                           for item in _models_for_profile(profile, configured, scope="serves", policy=policy))
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
            return _model_free(_effective_account_scope(account, "serves", model_id=_upstream_model(model, profile)), model, profile)
        return _model_free(_models_for_profile(profile, model_id=_upstream_model(model, profile)), model, profile)

    @classmethod
    def _entry_endpoint(cls, e: dict) -> str | None:
        """Return the credential's backend endpoint for isolated model availability checks."""
        profile = cls._entry_profile(e)
        return PROFILE_ENDPOINTS.get(profile) if profile else None

    @classmethod
    def _model_block_key(cls, entry):
        endpoint = cls._entry_endpoint(entry)
        if endpoint and cls._entry_profile(entry) in SHARED_INTL_PROFILES:
            identity = entry.get("account_key") or hashlib.sha256(str(entry.get("id", "")).encode()).hexdigest()
            return f"{endpoint}#account:{identity}"
        return endpoint


    def _model_servable(self, e: dict, model: str | None) -> bool:
        """Check backend/model backoff, skipping the check when no model is supplied."""
        if not model:
            return True
        endpoint = self._model_block_key(e)
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

    def _shard_revisions(self, policy=None) -> tuple:
        """Revisions that change static eligibility; any move invalidates every shard."""
        return (self.revision, model_policy.revision(CONFIG, policy), _catalog_revision,
                getattr(self._ledger, "routing_revision", 0))

    def _candidates(self, model: str | None, *, region=None, tried=(), policy=None) -> list[dict]:
        """Exclude tried credentials and rank candidates by zero rate and credit expiry."""
        tried = set(tried)
        # A request-scoped alias restricts the accounts, so its rule is part of the shard key.
        rule = model_policy.active_rule(CONFIG, model, policy)
        scope = (rule.get("public_id"), rule.get("region") or "", rule.get("profile") or "",
                 tuple(rule.get("credential_ids") or ()))
        eligible = self._scheduler.candidates(
            mode=RoutingScheduler.mode(CONFIG), revisions=(*self._shard_revisions(policy), scope),
            model=model, region=region, entries=self._entries,
            evaluate=lambda entry: self._eligible(entry, model, region=region, policy=policy))
        perf_metrics.inc("candidate_checks", len(eligible))
        healthy = [entry for entry in eligible if entry["cm"] not in tried
                   and self._healthy(entry, policy)
                   and self._model_healthy(entry, model)
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
             tried=(), with_capacity=False, requirements=None, policy=None) -> CredentialManager | None:
        """Select a healthy sticky or round-robin credential, preferring eligible zero-rate accounts."""
        # The candidate list comes from the indexes; file changes are applied by background reconcile.
        policy = policy or model_policy.view(CONFIG)  # One policy revision for the whole decision.
        started = time.perf_counter()
        waiting = time.perf_counter()
        self._lock.acquire()
        perf_metrics.observe("pool_lock_wait", time.perf_counter() - waiting)
        try:
            self._evict_sticky()
            candidates = self._candidates(model, region=region, tried=tried, policy=policy)
            if not candidates:
                if skey:
                    self._sticky.pop(skey, None)
                return None
            if requirements is not None:
                free = self._model_free(candidates[0], model)
                checked = [(entry, requirements.violations(
                    model_capabilities.entry_model(sys.modules[__name__], entry, model)))
                    for entry in candidates if self._model_free(entry, model) == free]
                candidates = [entry for entry, failures in checked if not failures]
                if not candidates:
                    raise model_capabilities.capability_error([failure for _, failures in checked for failure in failures])
            limit = CONFIG.get("max_inflight_per_account", 0)
            if with_capacity and limit:
                free = self._model_free(candidates[0], model)
                candidates = [entry for entry in candidates if self._model_free(entry, model) == free
                              and self._capacity.count(self._capacity_key(entry)) < limit]
                if not candidates:
                    perf_metrics.inc("capacity_acquire_failures")
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
        finally:
            self._lock.release()
            perf_metrics.observe("route_pick_duration", time.perf_counter() - started)

    def headers_for(self, skey: str | None, model: str | None = None, *, region=None,
                    with_generation=False, tried=(), with_capacity=False, requirements=None, policy=None):
        """Recheck identity and atomically reserve account capacity before sending."""
        policy = policy or model_policy.view(CONFIG)
        capacity_race = False
        capability_failures = []
        for _ in range(max(1, len(self._entries))):
            cm = self.pick(skey, model, region=region, tried=tried, with_capacity=with_capacity,
                           requirements=requirements, policy=policy)
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
                entry = self._index_cm.get(cm)
                if entry is not None and cm._generation != entry.get("generation"):
                    self.reload([cm.path], reset=False)  # Ownership changed on disk: re-read one file.
                    entry = self._index_cm.get(cm)
                if (entry is not None and cm._generation == generation and self._healthy(entry, policy)
                        and self._eligible(entry, model, region=region, profile=profile, policy=policy)
                        and self._model_healthy(entry, model)):
                    if requirements is not None:
                        failures = requirements.violations(model_capabilities.entry_model(sys.modules[__name__], entry, model))
                        if failures:
                            capability_failures.extend(failures)
                            continue
                    if with_capacity:
                        lease = self._capacity.acquire(self._capacity_key(entry),
                            CONFIG.get("max_inflight_per_account", 0), cm, generation)
                        if lease is None:
                            perf_metrics.inc("capacity_acquire_failures")
                            capacity_race = True
                            continue
                        return lease, headers
                    return ((cm, generation) if with_generation else cm), headers
        if capacity_race:
            raise self._capacity_error()
        if capability_failures:
            raise model_capabilities.capability_error(capability_failures)
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

    def _note_error_body(self, cm, body, *, generation=None):
        """Keep the last upstream error JSON for the credentials tooltip; bounded, redacted."""
        # The diagnostic arrives pre-parsed from note_status; never re-parse under the lock.
        if not body:
            return
        with self._lock, (cm._lock if generation is not None else nullcontext()):
            if not self._lease_matches(cm, generation):
                return
            entry = self._index_cm.get(cm)
            if entry is not None:
                entry["last_error_body"] = body[:8192]

    def _auto_disable(self, cm, reason, *, generation=None):
        """A terminal upstream verdict: disable via the WebUI policy, not a cooldown."""
        entry = self._index_cm.get(cm)
        store = CONFIG.get("control_store")
        management = CONFIG.get("management")
        identity = entry.get("account_key") if entry else None
        if not identity or store is None or management is None:
            return
        try:
            with self._lock, (cm._lock if generation is not None else nullcontext()):
                if not self._lease_matches(cm, generation):
                    return
                store.disable_credential_auto(
                    identity, reason, body=entry.get("last_error_body") if entry else None,
                    at=time.time())
                self._sticky.clear()
                entry["auto_disabled"] = True
            management.gateway.invalidate_model_table()
        except Exception as error:
            # A control-store outage must never replace the upstream rejection being reported.
            _log(f"[cred] 自动禁用写入失败（{Path(cm.path).name}）: {sanitize_log_text(str(error), 200)}")
            return
        _log(f"[cred] {reason}，账号已自动禁用: {Path(cm.path).name}")

    def _note_fresh_401(self, cm, *, generation=None):
        """A 401 against a just-refreshed token means the account was revoked server-side."""
        try:
            age = cm.token_age_s()
        except Exception:
            return
        if age is None or age > 120:
            return
        with self._lock, (cm._lock if generation is not None else nullcontext()):
            if not self._lease_matches(cm, generation):
                return
            entry = self._index_cm.get(cm)
            if entry is None:
                return
            now = time.time()
            strikes = [t for t in entry.get("fresh_401s", []) if now - t < FRESH_401_WINDOW]
            strikes.append(now)
            entry["fresh_401s"] = strikes
            fresh = len(strikes) >= FRESH_401_LIMIT
            if fresh:
                entry["fresh_401s"] = []
        if fresh:
            self._auto_disable(cm, f"新令牌 {FRESH_401_WINDOW}s 内 {FRESH_401_LIMIT} 次被拒（401）",
                               generation=generation)
    def _note_refresh_reject(self, entry, error):
        """Count consecutive upstream refresh rejections; the refresh token itself is dead."""
        now = time.time()
        # A request-path refresh (get_headers -> _refresh) succeeding between rejections
        # proves the token alive; a long gap alone is not "consecutive" either.
        try:
            age = entry["cm"].token_age_s()
        except Exception:
            age = None
        refreshed_after = now - age if age is not None else 0.0
        if refreshed_after > entry.get("refresh_reject_at", 0.0) or \
                now - entry.get("refresh_reject_at", 0.0) > REFRESH_REJECT_WINDOW:
            entry["refresh_rejects"] = 0
        entry["refresh_reject_at"] = now
        rejects = entry.get("refresh_rejects", 0) + 1
        entry["refresh_rejects"] = rejects
        if error.body:
            entry["last_error_body"] = error.body[:8192]
        if rejects < REFRESH_REJECT_LIMIT:
            return
        entry["refresh_rejects"] = 0
        self._auto_disable(entry["cm"], "刷新令牌被上游连续拒绝")


    def cooldown(self, cm: CredentialManager, reason: str = "", *, generation=None):
        with self._lock, (cm._lock if generation is not None else nullcontext()):
            if not self._lease_matches(cm, generation):
                return
            for e in self._entries:
                if e["cm"] is cm:
                    e["fail_until"] = time.time() + CRED_COOLDOWN
                    e["last_error"] = sanitize_log_text(reason, 256)
                    e["last_failure_at"] = time.time()
                    self._remember_credential(e, e["last_error"])
        _log(f"[cred] 凭证熔断 {CRED_COOLDOWN}s: {Path(cm.path).name} {reason}")

    def note_status(self, cm: CredentialManager | None, status: int,
                    model: str | None = None, raw: bytes = b"", *, generation=None, retry_after=None,
                    channel_fallback=False):
        """Apply credential-wide auth cooldowns, per-model 429 cooldowns and backend/model backoff."""
        if cm is None:
            return
        if status in (401, 403):
            self.cooldown(cm, reason=f"backend HTTP {status}", generation=generation)
            # One parse feeds the audit body, the restriction check and the tooltip.
            code, _, body = _upstream_diagnostic(raw or b"")
            self._note_error_body(cm, body, generation=generation)
            if status == 403 and code in RESTRICTED_CODES:
                self._auto_disable(cm, f"上游限制 {code}", generation=generation)
            elif status == 401:
                self._note_fresh_401(cm, generation=generation)
            return
        if status == 400 and is_channel_check_error(raw):
            # Only a rejection that survived the console retry is account-wide; endpoints without a
            # console fallback (search, MCP) are left to request-scoped failover.
            if channel_fallback:
                self.cooldown(cm, reason="backend channel check (HTTP 400)", generation=generation)
            return
        not_servable = _parse_not_servable(raw, status) if model else None
        if not_servable:
            if channel_fallback:
                # The console catalog is this account's detour, so its rejection must not block the
                # shared domestic endpoint for accounts that still reach v2.
                self._cool_model(cm, model, time.time() + MODEL_COOLDOWN, generation=generation,
                                 reason=f"console {not_servable[0]}")
            else:
                self.note_not_servable(cm, model, code=not_servable[0], msg=not_servable[1], generation=generation)
            return
        if status != 429 or not model:
            return
        now = time.time()
        if retry_after is not None:
            until = now + retry_after
        else:
            reset = _parse_reset_time(raw)
            until = reset if reset is not None and reset > now else now + MODEL_COOLDOWN
        self._cool_model(cm, model, min(until, now + MODEL_COOLDOWN_MAX), generation=generation,
                         reason="HTTP 429")

    def _cool_model(self, cm, model: str, until: float, *, generation=None, reason: str = ""):
        """Cool one model on one credential, never shortening an existing cooldown."""
        now = time.time()
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
                    self._remember_model(e, routed_model, until)
        _log(f"[cred] 模型冷却 {model} @ {Path(cm.path).name} 至 "
             f"{time.strftime('%m-%d %H:%M:%S', time.localtime(until))} ({reason})")

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

    def note_not_servable(self, cm, model: str, code: str = "", msg: str = "", *, generation=None) -> float:
        """Isolate international model rejection by account; retain domestic backend backoff."""
        if not model:
            return 0.0
        with self._lock, (cm._lock if generation is not None else nullcontext()):
            if not self._lease_matches(cm, generation):
                return 0.0
            entry = next((e for e in self._entries if e["cm"] is cm), None)
            endpoint = self._model_block_key(entry) if entry else None
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
        endpoint = self._model_block_key(entry) if entry else None
        return bool(endpoint) and self._blocks.clear(endpoint, _block_model(model))

    def model_block_until(self, model: str | None, *, region=None) -> float | None:
        """Return a retry time only when every potential backend has confirmed backoff."""
        if not model:
            return None
        now = time.time()
        with self._lock:
            candidates = [e for e in self._entries
                          if self._healthy(e) and (region is None
                                                   or _in_region(self._entry_profile(e), region))
                          and model_policy.route_allowed(CONFIG, e, model)]
            endpoints = {self._model_block_key(e) for e in candidates}
            # Unknown catalogs remain potential sources, but cannot authorize dispatch.
            capable = {self._model_block_key(e) for e in candidates
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
            unknown = {self._model_block_key(e) for e in candidates if catalog_unknown(e)}
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
            cm = entry["cm"]
            failure = None
            refresh_reject = None
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
                except TokenRefreshRejected as error:
                    failure = (str(error), cm._generation)
                    # Keepalive rejections back off like every other keepalive failure;
                    # without this a 5-minute cadence stacks 3 strikes in ~10 minutes.
                    if keepalive_due:
                        entry["keepalive_after"] = now + CRED_KEEPALIVE_RETRY_S
                    # Deferred past cm._lock: the third rejection reaches _auto_disable,
                    # which takes the pool lock; pool paths acquire pool -> credential.
                    refresh_reject = error
                except Exception as error:
                    failure = (str(error), cm._generation)
                    if keepalive_due:
                        entry["keepalive_after"] = now + CRED_KEEPALIVE_RETRY_S
            if refresh_reject is not None:
                self._note_refresh_reject(entry, refresh_reject)
            if failure:
                self.cooldown(cm, reason=failure[0], generation=failure[1])
            elif refreshed:
                entry["refresh_rejects"] = 0
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

    def forget_cooldowns(self, entry):
        """Drop persisted cooldowns for a credential that no longer exists."""
        identity = entry.get("account_key")
        if identity:
            self._warn_storage("cooldown", self._cooldowns.forget(identity)["durable"])

    def forget_usage(self, entry):
        """Drop a deleted credential's cached usage, in the store and the live aggregate."""
        snapshots = CONFIG.get("usage_snapshots")
        if snapshots is not None:
            snapshots.forget(entry["id"])
        accounts = CONFIG.get("usage_daily_accounts")
        if isinstance(accounts, dict):
            accounts.pop(entry["id"], None)

    def forget_credential_state(self, entry):
        """Run both independent cleanups for a credential that is gone or replaced."""
        self.forget_cooldowns(entry)
        self.forget_usage(entry)

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

CRED_RECONCILE_INTERVAL_S = 5   # Credential directory polling interval for background reconcile


def _reconcile_loop(pool: CredentialPool):
    """Apply credential file changes in the background so requests never scan the pool."""
    try:
        previous = pool.fingerprint()
    except Exception:
        previous = None
    while True:
        time.sleep(CRED_RECONCILE_INTERVAL_S)
        try:
            previous = pool.reconcile(previous)
        except Exception as e:
            _log(f"[cred] 目录变更处理异常: {_network_error_text(e)}")

CHECKIN_FIRST_DELAY = 30     # Initial check-in delay in seconds
HOUSEKEEP_INTERVAL = 3600    # Account maintenance interval in seconds
HOUSEKEEP_MIN_SYNC_GAP_S = 300   # Skip accounts synced this recently, so event cycles never repeat them
HOUSEKEEP_CONCURRENCY = 4        # Default bounded workers per upstream endpoint family
_ENDPOINT_KINDS = ("credits", "models", "usage")
_endpoint_limits: dict = {}
_ENDPOINT_LIMIT_LOCK = threading.Lock()  # Workers initialize semaphores concurrently.
_published_catalog_signature: str | None = None  # Last published catalog content; skips no-op bumps.


def _housekeep_concurrency() -> int:
    """Resolve the bounded worker count; 1 restores the previous serial behaviour."""
    value = CONFIG.get("housekeep_concurrency", HOUSEKEEP_CONCURRENCY)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else HOUSEKEEP_CONCURRENCY


def _endpoint_semaphore(kind: str):
    """One independent limit per upstream endpoint family, so no phase starves another."""
    limit = _housekeep_concurrency()
    with _ENDPOINT_LIMIT_LOCK:
        if _endpoint_limits.get("limit") != limit:
            _endpoint_limits.clear()
            _endpoint_limits["limit"] = limit
            for name in _ENDPOINT_KINDS:
                _endpoint_limits[name] = threading.BoundedSemaphore(limit)
        return _endpoint_limits[kind]


def _bounded_map(function, items, limit):
    """Run function over items on a bounded worker pool, yielding results in input order."""
    items = list(items)
    if limit <= 1 or len(items) <= 1:
        for item in items:
            yield function(item)
        return
    with ThreadPoolExecutor(max_workers=min(limit, len(items)), thread_name_prefix="housekeep") as workers:
        yield from workers.map(function, items)


def _bearer_token(headers: dict) -> str:
    return (headers.get("Authorization") or "").removeprefix("Bearer ").strip()


_HOUSEKEEP_LOCK = threading.Lock()


def _sync_error(pool, ledger, entry, generation, phase, error):
    message = f"{phase}: {_network_error_text(error)}"
    pool.apply_if_current(entry["cm"], generation, lambda: ledger.note_error(entry["id"], message))
    _log(f"[{phase}] {Path(entry['id']).name} 同步失败（保留旧数据）: {message}")


def _daily_chat_done(entry, day):
    """Today's turn already confirmed, so the sweep must not spend another one."""
    store = CONFIG.get("control_store")
    if store is None or not entry.get("account_key"):
        return True
    try:
        return store.daily_chat_done(entry["account_key"], day)
    except Exception:
        # An unreadable record is treated as done: never guess a turn was free to send.
        return True


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
                # Read the identity from the credential itself, never rebuilt from headers: a
                # uid-less account identified by email would otherwise hash to a different key
                # here than the one the pool recorded, and its sync would be rejected forever.
                summary = cm.summary()
                if expected_identity is not None and summary.get("account_key") != expected_identity:
                    failed.add(cid)
                    return None
                headers = cm.get_headers()
            finally:
                generation = cm._generation
        profile = profile_for_headers(headers)
        identity = summary.get("account_key")
        if entry.get("account_key") and entry["account_key"] != identity:
            failed.add(cid)
            return None  # A path now owned by another account must be rescheduled with its own preferences.
        site = site_for_headers(headers)
        # The upstream uid header is used verbatim; the local identity above is the key.
        token, uid, domain = _bearer_token(headers), headers.get("X-User-Id", ""), headers.get("X-Domain", "")
        day = daily_chat.today()
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
        if not travel.supported(entry.get("profile"), entry.get("enterprise") is True):
            # The exemption is terminal, so keep the ledger honest instead of leaving the
            # previous upstream refusal as this account's current travel state. This is not
            # tied to check-in: a manual sync (checkin=False) must correct the row too.
            # A stored notice is only rewritten when it differs, so the sweep stays a no-op.
            notice = travel.unavailable(entry.get("profile"), entry.get("enterprise") is True)
            if not pool.apply_if_current(cm, generation, lambda: travel.clear_unsupported(ledger, cid, notice)):
                failed.add(cid)
                return None
        if checkin and model_policy.credential_auto_travel(CONFIG, entry):
            try:
                def can_travel():
                    return (model_policy.credential_auto_travel(CONFIG, entry)
                            and pool.apply_if_current(cm, generation, lambda: None))
                trip = travel.perform(token, profile_for_headers(headers), can_write=can_travel,
                                      enterprise=entry.get("enterprise") is True,
                                      buddy_context=_buddy_context(entry, headers))
                if not pool.apply_if_current(cm, generation, lambda: travel.remember(ledger, cid, trip)):
                    failed.add(cid)
                    return None
                buddy.daily_warning(CONFIG, entry.get("account_key"), entry.get("profile"), trip)
            except Exception as error:
                _sync_error(pool, ledger, entry, generation, "travel", error)
        if not model_policy.credential_enabled(CONFIG, entry):
            return None
        # A-layer growth sweep: pure accept/claim, no behavior chains, so it rides the
        # hourly pass. Enabled chains (growth_auto switches) are driven by the same
        # module once their behavior layers land; the claim layer alone needs no consent.
        if checkin and model_policy.credential_auto_growth(CONFIG, entry):
            try:
                summary = growth_tasks.sweep_account(token, uid=uid, domain=domain)
                if summary.get("claimed") or summary.get("errors"):
                    _log(f"[growth] {Path(cid).name}: claimed={len(summary.get('claimed') or [])} "
                         f"accepted={len(summary.get('accepted') or [])} +{summary.get('credit', 0)}c "
                         f"+{summary.get('energy', 0)}e errors={summary.get('errors', 0)}")
            except Exception as error:
                _sync_error(pool, ledger, entry, generation, "growth", error)
            enabled = model_policy.growth_auto_tasks(CONFIG, entry) & set(growth_chains.RUNNERS)
            chain_base = PROFILE_ENDPOINTS[profile_for_headers(headers)]
            for code in sorted(enabled):
                try:
                    ctx = growth_chains.Context(
                        base=chain_base, token=token, uid=uid, domain=domain,
                        headers=headers, lock_key=entry.get("account_key") or cid)
                    outcome = growth_chains.run(code, ctx)
                    if outcome.get("skipped") or outcome.get("claimed"):
                        _log(f"[growth] {Path(cid).name} {code}: {outcome.get('message', '')}")
                except Exception as error:
                    _sync_error(pool, ledger, entry, generation, f"growth:{code}", error)
        if not model_policy.credential_enabled(CONFIG, entry):
            return None
        # A daily-activity turn spends credits on the account and is opt-in, so it runs
        # before the balance refresh reads back whatever it cost. Each account waits for
        # its own slot inside the day so the accounts never go out together.
        if (checkin and model_policy.credential_auto_daily_chat(CONFIG, entry)
                and not _daily_chat_done(entry, day)
                and daily_chat.is_due(entry.get("account_key"), day)):
            try:
                def can_chat():
                    return (model_policy.credential_auto_daily_chat(CONFIG, entry)
                            and pool.apply_if_current(cm, generation, lambda: None))
                turn = daily_chat.perform(token, profile, uid=uid, domain=domain, can_write=can_chat,
                                          store=CONFIG.get("control_store"),
                                          identity=entry.get("account_key"))
                _log(f"[dailychat] {Path(cid).name}: state={turn.get('state')} ok={turn.get('ok')}")
            except Exception as error:
                _sync_error(pool, ledger, entry, generation, "dailychat", error)
        if not model_policy.credential_enabled(CONFIG, entry):
            return None
        # An enterprise account always bills through the enterprise endpoint, so an
        # unresolvable tenant must fail loudly rather than silently fall back to the
        # personal endpoint, which would report it as having no credits at all.
        # A credit lookup can fail independently of the account catalog. Keep the old
        # balance, record the failure for retry, and still return the valid credential
        # lease so catalog synchronization can proceed.
        try:
            enterprise_id = _enterprise_dispatch_id(token, _credential_account(cm._session()),
                                                   uid=uid, domain=domain)
            balance = credits_mod.fetch_credits(
                token, uid=uid, domain=domain,
                # The dispatch key travels from the credential so both views agree.
                enterprise_id=enterprise_id or "")
            if bool(balance.get("intl")) != (site == INTERNATIONAL):
                raise ValueError("积分响应与凭据站点不一致")
            changes = []
            if not pool.apply_if_current(cm, generation,
                                         lambda: changes.extend(ledger.update_credits(cid, balance) or ())):
                failed.add(cid)
                return None
            _record_credit_changes(entry.get("account_key"), changes)
            shown = "无限" if balance.get("unlimited") and balance.get("enterprise") else balance["credits"]
            _log(f"[credits] {Path(cid).name}: 站点 {site}，余额 {shown}")
        except credits_mod.AuthExpiredError:
            raise
        except Exception as error:
            _sync_error(pool, ledger, entry, generation, "credits", error)
            failed.add(cid)
        profile = profile_for_headers(headers)
        return entry, generation, headers, profile
    except Exception as error:
        failed.add(cid)
        _sync_error(pool, ledger, entry, generation, "credits", error)
        return None


def _record_credit_changes(identity, changes):
    """Write each balance change to the audit log; auditing never fails a balance refresh."""
    audit = CONFIG.get("audit_store")
    if audit is None or not identity:
        return
    for change in changes:
        try:
            audit.event("credit", "credit." + change["type"], dict(change, credential=identity))
        except Exception:
            pass


def _publish_model_cache():
    """Publish account-scoped versioned catalogs, excluding ownerless shared caches."""
    global _published_catalog_signature
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
            catalogs = {profile: None for profile in PROFILE_ENDPOINTS}
            for account in accounts.values():
                if account["models"] is not None:
                    models = catalogs[account["profile"]]
                    if models is None:
                        models = catalogs[account["profile"]] = []
                    models.extend(account["models"])
            # Token refreshes republish identical content; only a real change may bump the
            # catalog revision, or steady-state shards would rebuild on every maintenance write.
            signature = json.dumps(accounts, sort_keys=True, default=str)
            if (signature != _published_catalog_signature
                    or CONFIG.get("account_catalogs") is None):
                # The signature alone is not enough: a rebuilt pool or a fresh process may
                # hold identical content while account_catalogs was never published.
                _published_catalog_signature = signature
                CONFIG["account_catalogs"] = accounts
                CONFIG["model_catalogs"] = catalogs
                CONFIG["models_remote"], CONFIG["models_intl"] = catalogs["cn-cli"], catalogs["intl-cli"]
                invalidate_model_table()  # Drop the stale table first, then install the fresh index.
                _publish_catalog_index(accounts)


def _account_catalog_key(profile, headers):
    return catalog_cache_key(profile, account_key(profile, headers.get("X-User-Id"),
                                                  headers.get("X-Enterprise-Id")))


def _fetch_catalog(headers):
    """Fetch both catalog scopes for one account token; the only upstream call involved."""
    with _endpoint_semaphore("models"):
        scopes = credits_mod.fetch_model_scopes(
            _bearer_token(headers), domain=headers.get("X-Domain", ""),
            uid=headers.get("X-User-Id", ""), enterprise_id=headers.get("X-Enterprise-Id", ""))
    return scopes["picker"], scopes["account"]


def _store_catalog(pool, cache, entry, generation, key, models, serves):
    """Publish one fetched catalog only while the credential lease is still current."""
    def publish():
        cache.put(key, models, serves=serves)
        for current in pool._entries:
            if current["cm"] is entry["cm"]:
                current["catalog_dirty"] = False
    return pool.apply_if_current(entry["cm"], generation, publish)


def _sync_one_account_catalog(pool, ledger, entry, generation, headers, profile, failed, cache):
    """Refresh one account's own catalog; True when a fetch was stored, None when skipped."""
    if not model_policy.credential_enabled(CONFIG, entry):
        return None
    key = _account_catalog_key(profile, headers)
    if cache.fresh(key) and not entry.get("catalog_dirty"):
        return None
    perf_metrics.inc("catalog_fetch_count")
    try:
        models, serves = _fetch_catalog(headers)
        if _store_catalog(pool, cache, entry, generation, key, models, serves):
            _log(f"[models] {profile} 模型表已刷新: 选择器 {len(models)} 个，"
                 f"账号根表 {len(serves)} 个")
            return True
        failed.add(entry["id"])
    except Exception as error:
        perf_metrics.inc("catalog_fetch_failures")
        failed.add(entry["id"])
        _sync_error(pool, ledger, entry, generation, "models", error)
    return False


def _sync_account_catalogs(pool, ledger, refs, failed, cache):
    """Refresh every synced account's own catalog; used by the account probe mode."""
    list(_bounded_map(lambda ref: _sync_one_account_catalog(pool, ledger, *ref, failed=failed, cache=cache),
                      list(refs.values()), _housekeep_concurrency()))


def _mark_catalog_diffs(pool, profile, model_ids, cache, previous_ids):
    """Mark accounts lacking models newly advertised since the last representative probe."""
    added = model_ids - previous_ids
    if not added:
        # Older catalog versions are expected in mixed pools; only genuine additions matter.
        return
    marked = []
    with pool._lock:
        for entry in pool._entries:
            identity, current = entry.get("account_key"), entry.get("profile")
            if not identity or current != profile:
                continue
            known = {item.get("id") for item in (cache.models(catalog_cache_key(profile, identity)) or [])}
            if known and not added <= known:
                entry["catalog_dirty"] = True
                marked.append(entry["id"])
    for cid in marked:  # Queue outside the lock so the wake-up can publish the model cache.
        pool._queue_sync(cid)


_LAGGARD_REFRESH_CAP = 16  # Per-cycle floor for back-filling missing or dirty account catalogs.


def _laggard_cap(total: int) -> int:
    """Scale the per-cycle cap so any backlog converges within about twelve cycles."""
    return max(_LAGGARD_REFRESH_CAP, -(-total // 12))


def _refresh_laggard_catalogs(pool, ledger, failed, cache):
    """Back-fill account-level catalogs in representative mode, where probing skips them.

    An account without its own catalog is unroutable, so missing catalogs come first and
    dirty accounts follow; progress wakes the next cycle instead of waiting an hour.
    """
    missing, dirty = [], []
    policy = model_policy.view(CONFIG)  # One snapshot for the whole loop; it holds the pool lock.
    with pool._lock:
        for entry in pool._entries:
            if (not entry.get("account_key") or entry.get("profile") is None
                    or not model_policy.credential_enabled(CONFIG, entry, policy)
                    or not pool._healthy(entry, policy)):
                continue
            known = cache.age(catalog_cache_key(entry["profile"], entry["account_key"])) is not None
            if not known:
                missing.append(entry)
            elif entry.get("catalog_dirty"):
                dirty.append(entry)
    backlog = missing + dirty
    cap = _laggard_cap(len(backlog))
    if not backlog:
        return
    refs = {}
    for entry in backlog[:cap]:
        cm = entry["cm"]
        try:
            # One broken account must not abort the pass before the model cache is published.
            with cm._lock:
                refs[entry["id"]] = (entry, cm._generation, cm.get_headers(), entry["profile"])
        except Exception as error:
            failed.add(entry["id"])
            _sync_error(pool, ledger, entry, entry.get("generation"), "models", error)
    results = list(_bounded_map(
        lambda ref: _sync_one_account_catalog(pool, ledger, *ref, failed=failed, cache=cache),
        list(refs.values()), _housekeep_concurrency()))
    if any(result for result in results) and len(backlog) > cap:
        # Only real progress re-wakes the loop; failures fall back to the hourly cycle.
        pool._sync_event.set()


def _probe_profile_catalog(pool, ledger, refs, failed, cache, aggregate, profile):
    """Probe at most one representative credential for this product profile."""
    candidates = [{"entry": entry, "generation": generation, "headers": headers,
                   "account_key": entry.get("account_key")}
                  for entry, generation, headers, candidate_profile in list(refs.values())
                  if candidate_profile == profile and entry.get("account_key")
                  and model_policy.credential_enabled(CONFIG, entry)]
    if not candidates:
        return  # This cycle has no synced account for the profile; no probe happened.
    target = aggregate.choose(
        profile, candidates, age_of=lambda candidate: cache.age(_account_catalog_key(profile, candidate["headers"])))
    if target is None:
        aggregate.fresh(profile)
        return
    entry, generation, headers = target["entry"], target["generation"], target["headers"]
    key = _account_catalog_key(profile, headers)
    perf_metrics.inc("catalog_fetch_count")
    try:
        models, serves = _fetch_catalog(headers)
        if not _store_catalog(pool, cache, entry, generation, key, models, serves):
            aggregate.failure(profile, source=entry.get("account_key"))
            failed.add(entry["id"])
            return
        previous_ids = aggregate.model_ids(profile)
        aggregate.success(profile, source=entry.get("account_key"), generation=generation,
                          models=[item.get("id") for item in models])
        _mark_catalog_diffs(pool, profile, {item.get("id") for item in models}, cache, previous_ids)
        _log(f"[models] {profile} 代表账号目录已刷新: 选择器 {len(models)} 个，"
             f"账号根表 {len(serves)} 个")
    except Exception as error:
        perf_metrics.inc("catalog_fetch_failures")
        failed.add(entry["id"])
        aggregate.failure(profile, source=entry.get("account_key"))
        _sync_error(pool, ledger, entry, generation, "models", error)


def _probe_profile_catalogs(pool, ledger, refs, failed, cache, aggregate):
    """Probe one representative credential per due product profile, each on its own slot."""
    due = [profile for profile in PROFILE_ENDPOINTS if aggregate.due(profile)]
    if not due:
        return
    list(_bounded_map(lambda profile: _probe_profile_catalog(pool, ledger, refs, failed, cache, aggregate, profile),
                      due, _housekeep_concurrency()))


def _sync_model_catalogs(pool, ledger, refs, failed):
    cache = CONFIG.get("model_cache")
    if cache is None:
        return
    aggregate = CONFIG.get("aggregate_catalog")
    if CONFIG.get("catalog_probe_mode", "account") == "representative" and aggregate is not None:
        _probe_profile_catalogs(pool, ledger, refs, failed, cache, aggregate)
        _refresh_laggard_catalogs(pool, ledger, failed, cache)
    else:
        _sync_account_catalogs(pool, ledger, refs, failed, cache)
    _publish_model_cache()


CATALOG_SCAN_START_LOCK = threading.Lock()   # One user-triggered full scan at a time.
CATALOG_SCAN_PROGRESS_LOCK = threading.Lock()  # Progress counters are shared by scan workers.
CATALOG_SCAN_MAX_ROWS = 500                  # Bounded per-account progress payload.
_SCAN_FIELDS = ("status", "total", "completed", "success", "failed", "differences",
                "started_at", "finished_at", "error", "profiles", "accounts")


def catalog_scan_view() -> dict:
    """Return the current or last full catalog scan without any credential material."""
    scan = CONFIG.get("catalog_scan")
    if not scan:
        return {"status": "idle", "total": 0, "completed": 0, "success": 0, "failed": 0,
                "differences": 0, "started_at": None, "finished_at": None, "error": None,
                "profiles": {}, "accounts": []}
    return {key: scan.get(key) for key in _SCAN_FIELDS}


def catalog_status() -> dict:
    """Describe probe mode, representative state and the last full scan."""
    aggregate = CONFIG.get("aggregate_catalog")
    return {"probe_mode": CONFIG.get("catalog_probe_mode", "account"),
            "representative": aggregate.view() if aggregate is not None else None,
            "scan": catalog_scan_view()}


def _scan_one_catalog(pool, cache, entry):
    """Refresh one account's catalog and report how far it moved from the cached copy."""
    cm = entry["cm"]
    with cm._lock:
        headers = cm.get_headers()
        generation = cm._generation
        profile = profile_for_headers(headers)
    key = _account_catalog_key(profile, headers)
    previous = {item.get("id") for item in (cache.models(key) or [])}
    models, serves = _fetch_catalog(headers)
    if not _store_catalog(pool, cache, entry, generation, key, models, serves):
        raise RuntimeError("扫描期间凭证已变化，已跳过该账号")
    current = {item.get("id") for item in models}
    # A first fetch has no baseline to diff against; only a change to a known catalog counts.
    differences = len(previous ^ current) if previous else 0
    return {"profile": profile, "models": len(models), "serves": len(serves),
            "differences": differences, "model_ids": [item.get("id") for item in models]}


def _scan_one_account(pool, cache, scan, aggregate, entry):
    """Refresh one account's catalog and fold its result into the shared progress record."""
    row = {"name": Path(entry["id"]).name}
    try:
        if not model_policy.credential_enabled(CONFIG, entry):
            row.update(status="skipped", reason="disabled")
        else:
            result = _scan_one_catalog(pool, cache, entry)
            row.update(status="ok", **{key: value for key, value in result.items() if key != "model_ids"})
            with CATALOG_SCAN_PROGRESS_LOCK:
                scan["success"] += 1
                scan["differences"] += result["differences"]
            if aggregate is not None and entry.get("account_key"):
                # A full scan satisfies this profile's TTL, so no probe follows immediately.
                aggregate.success(result["profile"], source=entry["account_key"],
                                  generation=entry.get("generation"), models=result["model_ids"])
    except Exception as error:
        perf_metrics.inc("catalog_fetch_failures")
        row.update(status="failed", error=sanitize_log_text(_network_error_text(error), 120))
        with CATALOG_SCAN_PROGRESS_LOCK:
            scan["failed"] += 1
    with CATALOG_SCAN_PROGRESS_LOCK:
        scan["completed"] += 1
        if len(scan["accounts"]) < CATALOG_SCAN_MAX_ROWS:
            scan["accounts"].append(row)


def _catalog_profile_versions(pool, cache) -> dict:
    """Summarize distinct catalog versions per profile after a scan, without identities."""
    versions: dict = {}
    with pool._lock:
        for entry in pool._entries:
            profile, identity = entry.get("profile"), entry.get("account_key")
            if not profile or not identity:
                continue
            ids = frozenset(item.get("id") for item in (cache.models(catalog_cache_key(profile, identity)) or []))
            if ids:
                versions.setdefault(profile, {})[ids] = versions.setdefault(profile, {}).get(ids, 0) + 1
    return {profile: {"accounts": sum(counts.values()), "distinct": len(counts),
                      "versions": sorted(({"models": len(ids), "accounts": count} for ids, count in counts.items()),
                                         key=lambda row: (-row["accounts"], row["models"]))[:5]}
            for profile, counts in versions.items()}


def _catalog_scan_worker(pool, cache, scan, entries):
    """Scan every account with bounded workers, keeping each previous catalog when a refresh fails."""
    aggregate = CONFIG.get("aggregate_catalog")
    try:
        list(_bounded_map(lambda entry: _scan_one_account(pool, cache, scan, aggregate, entry),
                          entries, _housekeep_concurrency()))
        _publish_model_cache()
        scan["profiles"] = _catalog_profile_versions(pool, cache)
        scan["status"] = "completed"
    except Exception as error:
        scan["status"] = "failed"
        scan["error"] = sanitize_log_text(_network_error_text(error), 120)
    finally:
        scan["finished_at"] = time.time()
        _log(f"[models] 手动全量扫描结束: 成功 {scan['success']}，失败 {scan['failed']}，"
             f"差异 {scan['differences']}")


def start_catalog_scan() -> dict:
    """Start one user-triggered full scan; an already running scan is reported instead."""
    pool, cache = CONFIG.get("cred_pool"), CONFIG.get("model_cache")
    if pool is None or cache is None or credits_mod is None:
        raise HTTPException(status_code=503, detail={"error": {
            "message": "模型目录不可用，无法扫描", "type": "service_unavailable",
            "code": "catalog_unavailable"}})
    with CATALOG_SCAN_START_LOCK:
        current = CONFIG.get("catalog_scan")
        if current is not None and current.get("status") == "running":
            return catalog_scan_view()
        entries = pool.entries()
        scan = {"status": "running", "total": len(entries), "completed": 0, "success": 0,
                "failed": 0, "differences": 0, "started_at": time.time(), "finished_at": None,
                "error": None, "profiles": {}, "accounts": []}
        CONFIG["catalog_scan"] = scan
        threading.Thread(target=_catalog_scan_worker, args=(pool, cache, scan, entries),
                         daemon=True, name="catalog-scan").start()
    return catalog_scan_view()


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
    list(_bounded_map(
        lambda entry: _sync_one_usage(pool, entry, accounts, stale, expected_identity),
        targets, _housekeep_concurrency()))
    _publish_usage_daily(pool, stale)
    return stale & target_ids


def _sync_one_usage(pool, entry, accounts, stale, expected_identity=None):
    """Refresh one account's usage snapshot on the usage endpoint semaphore."""
    if not model_policy.credential_enabled(CONFIG, entry):
        return
    try:
        cm = entry["cm"]
        with cm._lock:
            if expected_identity is not None and cm.summary().get("account_key") != expected_identity:
                stale.add(entry["id"])
                return
            headers = cm.get_headers()
            generation = cm._generation
        site = site_for_headers(headers)
        with _endpoint_semaphore("usage"):
            usage = credits_mod.fetch_request_usage(_bearer_token(headers), uid=headers.get("X-User-Id", ""),
                                                    domain=headers.get("X-Domain", ""))
        def store():
            accounts[entry["id"]] = {"identity": entry.get("account_key"), "site": site,
                                     "by_day": usage["by_day"],
                                     "total_credits": round(usage["total_credits"], 2),
                                     "requests": usage["requests"],
                                     "partial": bool(usage.get("partial")),
                                     "fetched_at": time.time()}
            snapshots = CONFIG.get("usage_snapshots")
            if snapshots is not None and entry.get("account_key") and entry.get("uid"):
                # Only a validated identity may key durable state. An absent flag defaults to
                # False, but a present value is passed through raw so the store's own strict
                # validator governs: coercing it here with bool() would mask a malformed value
                # and persist it as a legitimate flag.
                snapshots.store(entry["id"], entry["account_key"], site, usage,
                                partial=usage.get("partial", False))
        if not pool.apply_if_current(cm, generation, store):
            stale.add(entry["id"])
    except Exception as error:
        stale.add(entry["id"])
        _log(f"[usage] {Path(entry['id']).name} 明细拉取失败（保留其上次成功快照）: {_network_error_text(error)}")


def _usage_row_expired(snap) -> bool:
    """Whether a cached row is too old to keep showing."""
    return time.time() - float(snap.get("fetched_at") or 0.0) > USAGE_CACHE_MAX_AGE_S


def _adopt_cached_usage(pool, snapshots, accounts):
    """Seed the aggregate from the cache, keeping only rows that still belong to their account.

    Ownership is checked here, at the moment of use, rather than only when the cache was
    written: a path can be reused between restarts, and a row hydrated on an earlier pass
    would otherwise keep showing the previous account's usage indefinitely.
    """
    # The live map is mutated by forget_usage under the pool lock, so the reads below need that
    # same lock to stay consistent with a concurrent credential deletion or replacement.
    with pool._lock:
        cached = snapshots.accounts()
        if not cached and not accounts:
            return          # Nothing cached and nothing live: leave the pool untouched.
        # Only an account whose identity components are all present may own durable usage: an
        # account_key is a hash of the profile and UID, so an account with no UID would otherwise
        # share one identity with every other such account.
        owners = {entry["id"]: entry.get("account_key") for entry in pool.entries()
                  if entry.get("account_key") and entry.get("profile") and entry.get("uid")}
        # Drop any row whose recorded owner no longer matches the account at that path. This covers
        # rows hydrated by an earlier pass as well as live rows, so a reused path cannot keep
        # displaying the previous account's usage. Iterate over a copy of the items, so a row is
        # never looked up in the live map after the keys were copied.
        for path, row in list(accounts.items()):
            if owners.get(path) != row.get("identity"):
                accounts.pop(path, None)
        for path, row in cached.items():
            identity = row.get("identity")
            if owners.get(path) != identity:
                snapshots.forget(path)      # The path moved on; the cached row is not ours to show.
                continue
            if _usage_row_expired(row):
                snapshots.forget(path)
                continue
            # A live snapshot from this run always wins; the cache only fills a gap.
            if path in accounts:
                continue
            # A restored snapshot is stale until a refresh confirms it; clearing that is per account.
            accounts[path] = dict(row)
            accounts[path]["stale"] = True
        CONFIG["usage_daily_accounts"] = accounts


def _publish_usage_daily(pool, stale=()):
    """Aggregate enabled accounts' usage, retaining failed snapshots with explicit staleness."""
    # CredentialPool.forget_usage removes entries from the live map under the pool lock, so the
    # snapshot taken here and the pruning inside the loop must hold that same lock. Copying the
    # keys and then indexing the live dictionary would otherwise raise KeyError when a credential
    # is deleted or replaced while a usage-maintenance pass is running.
    with pool._lock:
        accounts = CONFIG.get("usage_daily_accounts")
        if not isinstance(accounts, dict):
            accounts = {}
            CONFIG["usage_daily_accounts"] = accounts
        # Seed from the on-disk cache so the dashboard is not blank until the first refresh.
        snapshots = CONFIG.get("usage_snapshots")
        if snapshots is not None:
            _adopt_cached_usage(pool, snapshots, accounts)
        enabled = {e["id"] for e in pool.entries() if model_policy.credential_enabled(CONFIG, e)}
        # Aggregate from a consistent local copy of the live map rather than indexing it per row.
        rows = dict(accounts)
        by_day, groups = {}, {}
        used, count = 0.0, 0
        partial = False          # Upstream paging hid additional usage for some account.
        stale_out = set()        # Accounts whose displayed figures are not from this run.
        newest = 0.0
        for cred_id, snap in rows.items():
            if cred_id not in enabled:
                continue
            if _usage_row_expired(snap):
                accounts.pop(cred_id, None)  # Drop it from the live map, not just from this sum.
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
            # `partial` is the aggregate "this view is incomplete" flag the dashboard shows, so a
            # failed or not-yet-refreshed account sets it; `stale_accounts` names which ones.
            if snap.get("partial") or snap.get("stale"):
                partial = True
            if snap.get("stale"):
                stale_out.add(Path(cred_id).name)
        # Failed enabled accounts must remain visible even without a prior snapshot.
        for cred_id in stale:
            if cred_id in enabled:
                partial = True
                stale_out.add(Path(cred_id).name)
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
        # Drop expired deadlines and aged usage so neither table grows without bound; a failed
        # prune is reported rather than silently leaving stale rows on disk.
        pool._cooldowns.prune()
        pool._warn_storage("cooldown", pool._cooldowns.last_error is None)
        if CONFIG.get("usage_snapshots") is not None:
            CONFIG["usage_snapshots"].prune()
        control_store = CONFIG.get("control_store")
        if control_store is not None:
            # Daily-activity rows are diagnostic only, so old ones are dropped rather
            # than kept as history; the day itself is still read back from this table.
            try:
                control_store.prune_daily_chats()
            except Exception as error:
                _log(f"[housekeeper] 打卡记录清理失败: {_network_error_text(error)}")
        media = CONFIG.get("media_store")
        if media is not None:
            try:
                removed = media.cleanup(ttl_hours=CONFIG.get("media_ttl_hours", 72),
                                        max_total_bytes=int(CONFIG.get("media_max_total_mb") or 1024) << 20)
                if removed.get("expired_or_evicted"):
                    _log(f"[housekeeper] 媒体清理：移除 {removed['expired_or_evicted']} 项资产、"
                         f"{removed.get('removed_files', 0)} 个文件")
            except Exception as error:
                _log(f"[housekeeper] 媒体清理失败: {_network_error_text(error)}")
        ids = pool.begin_sync(all_entries=not pending_only)
        failed = set()
        try:
            refs = {}
            targets = [entry for entry in pool.entries() if entry["id"] in ids]
            results = _bounded_map(
                lambda entry: _sync_account(pool, ledger, entry, checkin=not pending_only, failed=failed),
                targets, _housekeep_concurrency())
            for entry, result in zip(targets, results):
                if result is not None:
                    refs[entry["id"]] = result
            _sync_model_catalogs(pool, ledger, refs, failed)
            if not pending_only:
                _sync_usage(pool)
                if control_store is not None:
                    control_store.prune_rate_probes()
            # Billing samples arrive with every request; re-reading them each pass lets a
            # model that upstream started charging stop counting as free for routing.
            model_rates.refresh(sys.modules[__name__])
        except Exception:
            failed.update(ids)
            raise
        finally:
            pool.end_sync(ids, failed)


def _sync_account(pool, ledger, entry, *, checkin, failed):
    """One account's balance work, bounded by the credits endpoint semaphore."""
    with _endpoint_semaphore("credits"):
        return _sync_credits(pool, ledger, entry, checkin=checkin, failed=failed)


LEASE_INTERVAL_S = 30  # Running-instance heartbeat; offline tools treat older leases as stale.


def _lease_loop(store) -> None:
    """Refresh the running-instance lease without touching the settings revision."""
    while True:
        time.sleep(LEASE_INTERVAL_S)
        try:
            store.heartbeat_service_lease()
        except Exception:
            pass


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
    if anthropic_path(path):
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
                "admin_allowed_origins": "",  # Extra trusted management Origins (hot)
                "models_remote": None,   # Domestic cloud model inventory
                "models_intl": None,     # Eligible international model inventory
                "model_cache": None,     # Versioned catalog cache
                "model_catalogs": {},   # Display-only merged product catalogs
                "account_catalogs": None,  # Account-scoped models/serves; None enables legacy embedding
                "trial_ledger": None,
                "model_guard": True,     # Reject models absent from authorized catalogs
                "routing_scheduler": "shadow",  # legacy | shadow | indexed eligibility shards
                "catalog_probe_mode": "account",  # account | representative catalog refresh
                "housekeep_concurrency": 4,  # Bounded maintenance workers; 1 restores serial behaviour
                "max_images": 16, "image_policy": "truncate",
                "max_request_bytes": 32 * 1024 * 1024, "log_body_limit": 65536,
                "max_inbound_bytes": 64 * 1024 * 1024,
                "max_collect_bytes": 8 * 1024 * 1024, "max_concurrent": 64,
                "upstream_keepalive": False, "max_inflight_per_account": 0,
                "request_context_mode": "legacy",
                "stream_mode": "compatible", "model_capability_guard": True,
                "responses_projection_mode": "balanced", "responses_projection_max_bytes": 40000,
                "failover_max": 0,     # Credential failovers allowed before the first response byte
                "retry_write_timeout": False,  # Opt-in replay after incomplete writes
                "usage_daily": None,     # Usage aggregated by date and model
                "usage_daily_accounts": None,  # Independent per-account usage snapshots
                "usage_snapshots": None,  # On-disk cache of the per-account snapshots
                "credit_price_cny": None, "credit_price_usd": None, "usd_rate": None,
                "desensitize": False, "no_compact": False, "keep_tool_metadata": False}  # None prices use module defaults.

# In-memory OAuth sessions do not survive restarts.
_OAUTH = auth_oauth.OAuthManager(user_agent=USER_AGENT)


# ---------------------------------------------------------------------------
# Runtime audit events
# ---------------------------------------------------------------------------



def _log(msg: str):
    """Persist allowlisted runtime events in SQLite, never free-form text or secrets."""
    audit = CONFIG.get("audit_store")
    component = re.match(r"\[(cred|credits|models|usage|trial|checkin|dailychat|housekeeper)\]", msg)
    if audit is not None and component:
        # Persist event codes, not free-form lines which may contain upstream data.
        code = "cooldown" if "熔断" in msg or "冷却" in msg else "failure" if "失败" in msg or "异常" in msg else "updated"
        audit.event("runtime", component.group(1), {"code": code})


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


def _cred_for(payload: dict, model: str | None = None, *, region=None, tried=(), requirements=None,
              sticky=True):
    """Select a fresh credential lease and headers, excluding tried accounts; report unavailable capacity.

    ``sticky=False`` neither reads nor rebinds the session binding, for calls that serve a session
    without being its conversation (internal tool calls failing over from a borrowed account).
    """
    context = current_context()
    raw_key = None
    if sticky:
        raw_key = context.session_key if context is not None and context.scoped else session_key(payload)
    skey = f"{region}:{raw_key}" if raw_key and region is not None else raw_key
    skey = model_policy.sticky_scope(CONFIG, skey, model)
    pool = CONFIG.get("cred_pool")
    if pool is not None:
        resources = request_resources.get()
        picked = pool.headers_for(skey, model, region=region, with_generation=True, tried=tried,
                                  with_capacity=resources is not None, requirements=requirements)
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
    """Validate account capabilities and derive each routed body from canonical input."""
    context = current_context()
    protocol = context.protocol if context is not None else "chat"
    mode = thinking_mode(payload) if protocol == "messages" else None
    enabled = context.capability_guard if context is not None else CONFIG.get("model_capability_guard", True)
    cred = None
    try:
        requirements = model_capabilities.Requirements.from_request(body, payload, protocol) if enabled else None
        cred, headers = _cred_for(payload, body.get("model"), tried=tried, requirements=requirements)
        profile = profile_for_headers(headers)
        routed_model = _upstream_model(body.get("model"), profile)
        metadata = None
        if mode in ("enabled", "adaptive") or (requirements is not None and CONFIG.get("cred_pool") is None):
            entry = {"profile": profile, "account_key": account_key(
                profile, headers.get("X-User-Id"), headers.get("X-Enterprise-Id"))}
            metadata = model_capabilities.entry_model(sys.modules[__name__], entry, body.get("model"))
        if requirements is not None and CONFIG.get("cred_pool") is None:
            failures = requirements.violations(metadata)
            if failures:
                raise model_capabilities.capability_error(failures)
        canonical = body
        effort = resolve_reasoning_effort(body.get("reasoning_effort"), mode, metadata)
        if effort != body.get("reasoning_effort"):
            body = {**body, "reasoning_effort": effort}
        if routed_model != body.get("model"):
            body = {**body, "model": routed_model}
        body, merged_runs, merged_messages = merge_intl_user_images(body, profile)
        if body is not canonical:
            _guard_request_size(body)
        if merged_runs:
            observe_attempt("intl_image_merge", merged_runs=merged_runs, merged_messages=merged_messages)
        url = chat_url_for_headers(headers)
        observe_route(public_model=payload.get("model", "auto"), upstream_model=routed_model,
                      profile=profile, credential=account_key(profile, headers.get("X-User-Id"),
                                                             headers.get("X-Enterprise-Id")))
        _log(f"[{rid}] ROUTE | region={profile_region(profile)} | profile={profile} | model={routed_model} | url={url}")
        return body, cred, headers, url
    except HTTPException as error:
        release_credential(cred)
        detail = error.detail.get("error", {}) if isinstance(error.detail, dict) else {}
        observe_attempt("request_preflight_rejected", code=detail.get("code"))
        raise


def _note_cred_model_ok(cred, model: str | None) -> None:
    """Clear backend/model backoff after an upstream HTTP 200 response."""
    pool = CONFIG.get("cred_pool")
    if pool is not None and cred is not None and model:
        cm = cred[0] if isinstance(cred, tuple) else cred
        pool.note_model_ok(cm, model)


def _note_cred_status(cred, status: int, model: str | None = None, raw: bytes = b"", *, retry_after=None,
                      from_channel_fallback=False):
    """Record generation-scoped authentication, quota and unsupported-model failures."""
    pool = CONFIG.get("cred_pool")
    if pool is not None and cred is not None:
        cm, generation = cred if isinstance(cred, tuple) else (cred, None)
        pool.note_status(cm, status, model=model, raw=raw, generation=generation, retry_after=retry_after,
                         channel_fallback=from_channel_fallback)


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
                    # Imports and logins always re-verify the catalog, matching relogin semantics.
                    entry = pool.entry_by_id(str(target))
                    if entry is not None:
                        entry["catalog_dirty"] = True
                        pool._queue_sync(str(target))
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
                         unbind: bool = False,
                         authorization: Optional[str] = Header(default=None),
                         x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Delete the named .info file, unbinding model rules only after removal succeeds."""
    _check_admin_auth(authorization, x_api_key)
    pool = CONFIG.get("cred_pool")
    rollback = {}
    identity = None
    management = CONFIG.get("management")
    # Serialize with model-rule edits so a rebind cannot slip between unbind and removal.
    with CONFIG.get("admin_mutation_lock") or nullcontext():
        if management is not None:
            identity = management.admin_delete_guard(os.path.basename(name), unbind=unbind)
            if identity is not None:
                rollback = management.admin_unbind_credential(identity)
        if pool is None or not pool.remove_file(os.path.basename(name)):
            # A failed removal rolls the unbind back so routing never widens silently.
            if management is not None and rollback:
                management.admin_restore_bindings(rollback)
            raise HTTPException(status_code=404, detail={"error": {"message": f"凭据不在池中: {name}", "type": "invalid_request_error"}})
        if identity is not None and management is not None:
            # Sweep any rebind that raced the removal from outside the shared lock.
            management.admin_unbind_credential(identity)
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
    except ValueError:
        raise HTTPException(400, "请求体必须是有效 JSON 对象") from None
    if body:
        if action != "travel":
            raise HTTPException(400, "该操作不接受请求体")
        if (set(body) != {"confirm_buddy", "agreement_revision"}
                or body["confirm_buddy"] is not True or not isinstance(body["agreement_revision"], str)):
            raise HTTPException(400, "首领确认参数无效")
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
            # Unlimited enterprise tenants stay outside the monetary conversion.
            "unlimited_accounts": int(agg.get("unlimited_accounts") or 0),
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
_configured_cache: dict = {}   # (region, pool revision, policy revision) -> enabled profiles
_catalog_revision = 0


def invalidate_model_table() -> None:
    """Drop the public model snapshot and derived catalog index after catalog changes."""
    global _model_table_cache
    _model_table_cache = {}
    CONFIG["catalog_index"] = None  # Rebuilt from the live accounts on the next catalog read.


def _identity_enabled(identity) -> bool:
    """Whether a published account identity still has an enabled credential."""
    pool = CONFIG.get("cred_pool")
    entries = pool.entries_by_identity(identity) if pool is not None else ()
    if not entries:
        return True
    return any(model_policy.credential_enabled(CONFIG, entry) for entry in entries)


def _publish_catalog_index(accounts):
    """Build the read-only catalog index once per publish, never per request."""
    global _catalog_revision
    _catalog_revision += 1
    index = CatalogIndex(accounts, revision=_catalog_revision,
                         policy_revision=model_policy.revision(CONFIG),
                         is_enabled=_identity_enabled, scope_fn=_account_scope)
    CONFIG["catalog_index"] = index
    return index


def _catalog_index():
    """Return the catalog index matching the published accounts and policy revision."""
    accounts = CONFIG.get("account_catalogs")
    if accounts is None:
        return None
    index = CONFIG.get("catalog_index")
    if (index is not None and index.accounts is accounts
            and index.policy_revision == model_policy.revision(CONFIG)):
        return index
    return _publish_catalog_index(accounts)


def _catalog_for(profile: str, scope: str = "models", *, model_id=None):
    """Return a profile catalog within the requested account scope."""
    accounts = CONFIG.get("account_catalogs")
    if accounts is not None or CONFIG.get("model_cache") is not None:
        index = _catalog_index()
        if index is not None:
            return index.catalog(profile, scope, model_id=model_id)
        pool = CONFIG.get("cred_pool")
        models = None
        for entry in pool.entries() if pool is not None else []:
            if entry.get("profile") != profile:
                continue
            account = (accounts or {}).get(entry.get("account_key")) or {}
            items = _effective_account_scope(account, scope, model_id=model_id)
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


def _configured_profiles(region: str | None, policy=None) -> set[str]:
    """Return enabled profiles in a region, cached until the pool or policy revision moves."""
    pool = CONFIG.get("cred_pool")
    if pool is not None:
        key = (region, getattr(pool, "revision", 0), model_policy.revision(CONFIG, policy))
        cached = _configured_cache.get(key)
        if cached is not None:
            return cached
        profiles = frozenset(profile for entry in pool.entries() if (profile := pool._entry_profile(entry))
                             and model_policy.credential_enabled(CONFIG, entry, policy)
                             and _in_region(profile, region))
        _configured_cache.clear()
        _configured_cache[key] = profiles
        return profiles
    cm = CONFIG.get("cred")
    if cm is not None:
        profile = cm.summary()["profile"]
        return {profile} if _in_region(profile, region) else set()
    known = {profile for profile in PROFILE_ENDPOINTS
             if _in_region(profile, region) and _catalog_for(profile) is not None}
    return known or ({"intl-cli"} if region == "intl" else {"cn-cli"})


def _account_scope(account, scope="models"):
    """Account catalog with effective rates: manual override, measured billing, then declared."""
    items = _catalog_account_scope(account, scope)
    book = CONFIG.get("rate_book")
    return items if book is None else book.overlay(account.get("profile"), items, _free_multiplier)


def _usable_models(models):
    return [model for model in models or [] if model.get("id") and model.get("supportsToolCall")
            and not model.get("disabled")]


def _shared_catalog_sources(scope="models"):
    accounts, pool = CONFIG.get("account_catalogs"), CONFIG.get("cred_pool")
    if accounts is not None or CONFIG.get("model_cache") is not None:
        return [(entry["profile"], _account_scope(account, scope))
                for entry in (pool.entries() if pool is not None else [])
                if entry.get("profile") in SHARED_INTL_PROFILES and model_policy.credential_enabled(CONFIG, entry)
                and (account := (accounts or {}).get(entry.get("account_key")))
                and account.get("profile") == entry["profile"] and account.get("models") is not None]
    configured = _configured_profiles(None)
    return [(profile, _catalog_for(profile, scope)) for profile in sorted(configured & SHARED_INTL_PROFILES)]


def _effective_account_scope(account, scope="models", *, model_id=None):
    # Account-level scopes stay live so a capability change is rechecked inside a request;
    # the published profile catalogs below come from the indexed snapshot.
    native = _account_scope(account, scope)
    profile = account.get("profile")
    if profile not in SHARED_INTL_PROFILES or native is None:
        return native
    return share_models(native, profile, _shared_catalog_sources(scope), model_id=model_id)



def _published_account_scope(account, scope="models", *, model_id=None):
    """Per-account catalog for a published table: indexed at publish time, live as a fallback."""
    index = _catalog_index()
    if index is not None:
        items = index.account_catalog(account, scope)
        if items is not None:
            return items if model_id is None else [item for item in items if item.get("id") == model_id]
    return _effective_account_scope(account, scope, model_id=model_id)


def _models_for_profile(profile: str, configured=None, *, scope: str = "models", model_id=None,
                        policy=None) -> list[dict]:
    models = _catalog_for(profile, scope, model_id=model_id)
    if models is None:
        # Static fallback is limited to legacy domestic CLI deployments.
        configured = (_configured_profiles(profile_region(profile), policy)
                      if configured is None else configured)
        return ([{"id": name, "supportsToolCall": True} for name in DEFAULT_MODELS]
                if CONFIG.get("model_cache") is None and CONFIG.get("account_catalogs") is None
                and profile == "cn-cli" and configured <= {"cn-cli"} else [])
    if profile in SHARED_INTL_PROFILES and CONFIG.get("account_catalogs") is None and CONFIG.get("model_cache") is None:
        models = share_models(models, profile, _shared_catalog_sources(scope), model_id=model_id)
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


def _model_profiles(model: str | None, region: str | None = None, configured=None, *, policy=None) -> set[str]:
    configured = _configured_profiles(region, policy) if configured is None else configured
    profiles = {profile for profile in PROFILE_ENDPOINTS if _in_region(profile, region)}
    if not model:
        return profiles
    supported = {profile for profile in profiles
                 if any(item["id"] == _upstream_model(model, profile)
                        for item in _models_for_profile(profile, configured, scope="serves",
                                                        model_id=_upstream_model(model, profile), policy=policy))}
    if model == "auto" and region == "cn":
        # WorkBuddy uses its advertised Auto; legacy CLI defaults remain separate.
        if "cn-work" in configured and "cn-work" in supported:
            return {"cn-work"}
        if "cn-cli" in configured and _models_for_profile("cn-cli", configured, policy=policy):
            return {"cn-cli"}
    if (model == "auto" and region is None and "cn-cli" in configured
            and _models_for_profile("cn-cli", configured, policy=policy)):
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

        def _positive(entry):
            c = entry.get("credits") or {}
            if not c.get("intl"):
                return False
            if c.get("unlimited") and c.get("enterprise"):
                return True  # Unlimited tenants hold no numeric balance.
            return float(c.get("credits") or 0) > 0

        return bool(ledger and any(_positive(entry) for entry in ledger.snapshot().values()))
    return any(pool._entry_profile(entry) == profile and pool._has_credit(entry, profile)
               for entry in pool.entries() if model_policy.credential_enabled(CONFIG, entry))


def _legacy_catalog_signature() -> tuple:
    """Cheap signature of legacy CONFIG catalogs, which are only ever replaced wholesale."""
    catalogs = CONFIG.get("model_catalogs")
    rows = (tuple(sorted((str(name), len(items or ())) for name, items in catalogs.items()))
            if isinstance(catalogs, dict) else ())
    remote, intl = CONFIG.get("models_remote"), CONFIG.get("models_intl")
    return (rows,
            id(remote) if isinstance(remote, list) else None, len(remote) if isinstance(remote, list) else 0,
            id(intl) if isinstance(intl, list) else None, len(intl) if isinstance(intl, list) else 0)


def _model_table_revisions() -> tuple:
    """Return the revisions that a published model table depends on."""
    pool = CONFIG.get("cred_pool")
    ledger = CONFIG.get("ledger")
    index = CONFIG.get("catalog_index")
    return (getattr(pool, "revision", 0), model_policy.revision(CONFIG),
            getattr(index, "revision", 0), getattr(ledger, "revision", 0),
            _legacy_catalog_signature())


def _model_table(name, region, build):
    """Reuse a published model table until a revision moves or the short TTL lapses."""
    _catalog_index()  # A rebuilt index bumps its revision; the key must see the fresh value.
    key = (name, region, *_model_table_revisions())
    cached = _model_table_cache.get(key)
    if cached is not None and cached[0] > time.monotonic():
        perf_metrics.inc("model_table_hits")
        return cached[1]
    value = build(region)
    _model_table_cache.clear()
    _model_table_cache[key] = (time.monotonic() + _MODEL_TABLE_TTL, value)
    perf_metrics.inc("model_table_builds")
    return value


def current_models(region: str | None = None) -> list[str]:
    """Merge models eligible for current accounts without region-specific client URLs."""
    return _model_table("models", region, _build_current_models)


def _build_current_models(region: str | None = None) -> list[str]:
    pool = CONFIG.get("cred_pool")
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
                models = _usable_models(_published_account_scope(account, "serves"))
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
    return _model_table("details", region, _build_current_model_details)


def _build_current_model_details(region: str | None = None) -> list[dict]:
    pool = CONFIG.get("cred_pool")
    details: dict[str, dict] = {}
    declarations = {}
    for name in current_models(region):
        details[name] = {"id": name, "credits": None, "credits_by_profile": {}}
    if pool is None:
        return [{**item, **model_capabilities.describe_models(())} for item in details.values()]
    with pool._lock:
        def record(profile: str, item: dict, *, zero: bool) -> None:
            name = item.get("id")
            if name not in details:
                return
            if zero and not _free_multiplier(item.get("credits")):
                return  # Empty accounts cannot supply paid model rates.
            declarations.setdefault(name, []).append((profile, item))
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
                for item in _usable_models(_published_account_scope(account, "serves")):
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
    return [{**item, **model_capabilities.describe_models(declarations.get(item["id"], []))} for item in details.values()]


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
    # Keep error paths tied to caller positions when the upstream system message moves.
    message_indices = list(range(len(messages)))
    # Upstreams reject developer roles; copy them as system messages without changing content.
    messages = [
        dict(message, role="system") if message.get("role") == "developer" else message
        for message in messages
    ]
    if messages[0].get("role") != "system":
        system_index = next((index for index, message in enumerate(messages) if message.get("role") == "system"), None)
        if system_index is None:
            messages = [{"role": "system", "content": "You are a helpful assistant."}, *messages]
            message_indices.insert(0, None)
        else:
            messages = [messages[system_index], *messages[:system_index], *messages[system_index + 1:]]
            message_indices.insert(0, message_indices.pop(system_index))
    body["messages"] = normalize_chat_messages(messages, message_indices=message_indices)
    body = normalize_tool_schemas(body)
    _normalize_tool_choice(body)
    body["stream"] = True
    body.setdefault("stream_options", {"include_usage": True})
    body = _chat_body_desensitize(body)
    _guard_request_size(body)
    return body


def _upstream_chat_body(body: dict) -> dict:
    """Exclude process-local metadata from both wire JSON and its byte budget."""
    return {key: value for key, value in body.items()
            if key is not _REQUEST_POLICY_KEY and not (isinstance(key, str) and key.startswith("_"))}


def _guard_request_size(body: dict) -> int:
    """Validate and measure upstream JSON bytes without truncating text or tool arguments."""
    size = 0
    limit = CONFIG["max_request_bytes"]
    try:
        for part in json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), allow_nan=False).iterencode(_upstream_chat_body(body)):
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
    _media_chat_guard(name)
    if not resolved:
        name = model_policy.resolve(CONFIG, name)
    model_policy.check_resolved(CONFIG, name)
    if not CONFIG.get("model_guard"):
        return
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
             "credits": item["credits"], "credits_by_profile": item["credits_by_profile"],
             **{key: item[key] for key in ("credits_estimated", "capabilities", "limits", "metadata_by_profile")
                if key in item}}
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
        raise _bad_json(e) from None

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
    # Gateway-executed web tools: hosted web_search tool types plus web_search_options.
    server_spec: dict = {}
    if isinstance(body.get("tools"), list) and body["tools"]:
        plain_tools, split_spec = server_tools.split_server_tools(body["tools"], openai_types=True)
        body["tools"], server_spec = server_tools.merge_injected(plain_tools, split_spec)
    if isinstance(payload.get("web_search_options"), dict) and server_tools.TOOL_SEARCH not in server_spec:
        options_spec = {"WebSearch": server_tools.search_config_from_options(
            payload["web_search_options"])}
        body["tools"], options_active = server_tools.merge_injected(body.get("tools") or [], options_spec)
        server_spec.update(options_active)
    if server_spec:
        body["_server_tools"] = server_spec
    hosted_choice = body.get("tool_choice")
    if isinstance(hosted_choice, dict) and server_tools.is_openai_search_type(hosted_choice.get("type")):
        body["tool_choice"] = ({"type": "function", "function": {"name": server_tools.TOOL_SEARCH}}
                               if server_tools.TOOL_SEARCH in server_spec else "auto")
    body = await run_in_threadpool(_prepare_chat_body, body, session_payload=payload)
    stream_policy = _snapshot_stream_policy("chat", body)
    observe_stream_mode(stream_policy.mode)

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
            return _stream_upstream(url, headers, _body_with_stream_policy(routed, stream_policy),
                                    model_name, t0, rid, cred=cred)
        return _routed_stream(payload, prepared, model_name, rid, t0, attempt,
                              body, cred, headers, url)

    # Aggregate upstream SSE for non-streaming clients.
    async def fetch(routed, cred, headers, url):
        result, _records = await _run_server_tool_chat(
            url, headers, routed, model_name, rid, cred, policy=stream_policy)
        return result
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
    calls = tool_calls or []
    choice = body.get("tool_choice")
    if choice == "none":
        return not calls
    if choice == "required":
        names = _declared_tool_names(body)
        return bool(calls) and all(call.get("function", {}).get("name") in names for call in calls)
    if isinstance(choice, dict):
        name = (choice.get("function") or {}).get("name") if isinstance(choice.get("function"), dict) else None
        return bool(calls) and all(call.get("function", {}).get("name") == name for call in calls)
    return True


def _declared_tool_names(body: dict) -> frozenset[str]:
    """Extract declared function names for conservative realtime identity boundaries."""
    names = set()
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            names.add(function["name"])
        elif isinstance(tool.get("name"), str):
            names.add(tool["name"])
    return frozenset(name for name in names if name)


def _tool_calls_healthy(tool_calls, body: dict | None = None, *, require_declarations=False) -> bool:
    """Validate unique calls, declared names and JSON-object arguments."""
    calls = tool_calls or []
    names = set(_declared_tool_names(body)) if body is not None else None
    seen_ids = set()
    for tc in calls:
        if not isinstance(tc, dict):
            return False
        call_id = tc.get("id")
        if (not isinstance(call_id, str) or not call_id.strip()
                or (require_declarations and call_id in seen_ids)
                or (require_declarations and tc.get("type") != "function")):
            return False
        if not require_declarations:
            seen_ids.add(call_id)
        seen_ids.add(call_id)
        fn = tc.get("function") or {}
        if not isinstance(fn, dict):
            return False
        name = fn.get("name") or ""
        arguments = fn.get("arguments") or ""
        if not isinstance(name, str) or not name.strip() or not isinstance(arguments, str) or not arguments.strip():
            return False
        if names is not None and ((require_declarations or names) and name not in names):
            return False
        try:
            decoded = json.loads(arguments)
        except (TypeError, ValueError, UnicodeError, RecursionError):
            return False
        if not isinstance(decoded, dict):
            return False
    if (require_declarations and body is not None
            and body.get("parallel_tool_calls") is False and len(calls) > 1):
        return False
    return True


def _validate_realtime_tools(tool_calls, body: dict, finish_reason=None, *, filtered=False):
    """Validate terminal state without regenerating or replaying the request.

    Explicit refusal/filter and length results are incomplete upstream outcomes,
    not malformed tool calls. Any tool bytes that are present still have to be
    structurally healthy, but a required/named choice need not be satisfied when
    the upstream legitimately stopped before producing a call.
    """
    if not _tool_calls_healthy(tool_calls, body, require_declarations=True):
        healthy = False
    elif finish_reason is None:
        # Realtime success terminals require an explicit upstream completion
        # marker; [DONE] alone remains compatible only on the legacy path.
        healthy = False
    elif filtered or finish_reason in ("length", "content_filter", "content-filter", "refusal"):
        healthy = True
    else:
        healthy = (_tool_choice_satisfied(tool_calls, body)
                   and (finish_reason == "tool_calls" if tool_calls else finish_reason != "tool_calls"))
    if healthy:
        return
    raw = {"error": {"message": "Invalid upstream tool_calls", "type": "upstream_error",
                     "code": "invalid_tool_calls"}}
    raise UpstreamResponseError(502, json.dumps(raw).encode("utf-8"))


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
    if reasoning:
        lines.append(_line({"reasoning_content": reasoning}))
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


async def _replay_merged_sse(merged: dict):
    """Replay a collected completion as SSE lines, each followed by its blank event separator."""
    for line in _chat_result_to_sse_lines(merged):
        yield line
        yield ""


def _network_error_text(error: Exception) -> str:
    return sanitize_log_text(f"{type(error).__name__}: {str(error).strip() or 'upstream transport failed'}", 512)

async def _coalesce_reasoning_sse(lines, *, max_bytes=0):
    """Replay one reasoning block for compatible mode; realtime skips this wrapper."""
    pending: list[str] = []
    template = None
    buffer_budget = StreamOutputBudget(max_bytes)

    def flush():
        nonlocal template, pending
        if not pending:
            return None
        source = template or {}
        payload = dict(source)
        choices = source.get("choices") or []
        transformed = []
        for index, raw_choice in enumerate(choices):
            if not isinstance(raw_choice, dict):
                transformed.append(raw_choice)
                continue
            choice = dict(raw_choice)
            if index == 0:
                delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
                delta = {key: value for key, value in delta.items() if key == "role"}
                delta["reasoning_content"] = "".join(pending)
                choice["delta"] = delta
                choice["finish_reason"] = None
            transformed.append(choice)
        payload["choices"] = transformed
        wire = "data: " + json.dumps(payload, ensure_ascii=False)
        template = None
        pending = []
        return wire

    async for raw_line in lines:
        embedded_boundary = isinstance(raw_line, str) and raw_line.endswith(("\n", "\r"))
        line = raw_line.rstrip("\r\n") if isinstance(raw_line, str) else raw_line
        if not line or not line.strip():
            if pending:
                continue
            yield line
            continue
        if not line.startswith("data:"):
            # SSE comments and event/control lines do not end a reasoning run.
            yield line
            if embedded_boundary:
                yield ""
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            flushed = flush()
            if flushed is not None:
                yield flushed
                yield ""
            yield line
            if embedded_boundary:
                yield ""
            continue
        try:
            event = json.loads(data)
        except (TypeError, ValueError):
            flushed = flush()
            if flushed is not None:
                yield flushed
                yield ""
            yield line
            if embedded_boundary:
                yield ""
            continue
        choices = event.get("choices") if isinstance(event, dict) else None
        choice = choices[0] if isinstance(choices, list) and choices else None
        delta = choice.get("delta") if isinstance(choice, dict) else None
        reasoning = delta.get("reasoning_content") if isinstance(delta, dict) else None
        if isinstance(reasoning, str) and reasoning:
            if template is None:
                template = event
            pending.append(reasoning)
            buffer_budget.charge_text(reasoning)
            visible = any(delta.get(key) for key in ("content", "refusal", "tool_calls", "function_call"))
            visible = visible or bool(choice.get("finish_reason"))
            if not visible:
                continue
            flushed = flush()
            if flushed is not None:
                yield flushed
                yield ""
            transformed_choices = []
            for index, raw_choice in enumerate(choices):
                if not isinstance(raw_choice, dict):
                    transformed_choices.append(raw_choice)
                    continue
                clean_choice = dict(raw_choice)
                if index == 0:
                    clean_delta = dict(delta)
                    clean_delta.pop("reasoning_content", None)
                    clean_choice["delta"] = clean_delta
                transformed_choices.append(clean_choice)
            event = dict(event)
            event["choices"] = transformed_choices
            if any(isinstance(item, dict) and
                   (item.get("delta") or item.get("finish_reason") is not None)
                   for item in transformed_choices):
                yield "data: " + json.dumps(event, ensure_ascii=False)
                if embedded_boundary:
                    yield ""
            continue
        flushed = flush()
        if flushed is not None:
            yield flushed
            yield ""
        yield line
        if embedded_boundary:
            yield ""



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
        async with open_backend_stream(url, headers, _upstream_chat_body(body), read_timeout=timeout, on_retry=retry,
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


def _check_upstream_status(status, raw, cred, model, *, headers=None, from_channel_fallback=False):
    if status != 200:
        retry_after = parse_retry_after((headers or {}).get("Retry-After"))
        code, message, error_body = _upstream_diagnostic(raw)
        observe_attempt("upstream_error", status_code=status, upstream_code=code,
                        upstream_msg=message, upstream_body=error_body)
        if not is_filter_error(raw):
            _note_cred_status(cred, status, model=model, raw=raw, retry_after=retry_after,
                              from_channel_fallback=from_channel_fallback)
        raise UpstreamHTTPError(status, raw, retry_after=retry_after,
                                from_channel_fallback=from_channel_fallback)


@asynccontextmanager
async def _checked_chat_stream(url, headers, body, *, rid, model_name, cred):
    """Open a chat stream that is HTTP 200, retrying once on /console after a v2 channel rejection."""
    fallback_url = channel_fallback_url(url)
    from_channel_fallback = False
    while True:
        async with _backend_stream(url, headers, body, rid=rid, model_name=model_name) as response:
            if response.status_code == 200:
                _note_cred_model_ok(cred, body.get("model"))
                yield response
                return
            raw = await read_bounded_error(response)
            if should_channel_fallback(fallback_url, response.status_code, raw):
                observe_attempt("channel_check_fallback", status_code=response.status_code,
                                profile=profile_for_headers(headers), upstream_model=body.get("model"))
                _log(f"[{rid}] 上游渠道校验，切换 /console 端点重试 | {model_name}")
                url, fallback_url, from_channel_fallback = fallback_url, None, True
                continue
            _check_upstream_status(response.status_code, raw, cred, body.get("model"),
                                   headers=response.headers, from_channel_fallback=from_channel_fallback)


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


def _note_dsml_observation(rid, repairer):
    """Mirror the panel's two-line convention: distinguish 'not seen' from 'seen but rejected'."""
    if repairer.converted:
        observe_attempt("dsml_repaired", calls=repairer.converted, blocks=repairer.seen, known=repairer.known)
        _log(f"[{rid}] 🔧 从正文还原 {repairer.converted} 个原生工具调用 (known={repairer.known})")
    elif repairer.seen:
        observe_attempt("dsml_rejected", blocks=repairer.seen, known=repairer.known)
        _log(f"[{rid}] ⚠ 识别到 {repairer.seen} 个原生标记块但未还原 (known={repairer.known})")
    if repairer.saw_opensource:
        observe_attempt("native_markup_opensource", known=repairer.known)


async def _fetch_checked_chat(url, headers, body, model_name, rid, cred=None, *, filter_retry=False,
                             max_collect_bytes=None):
    """Collect with bounded repair and one eligible filter retry using a frozen request budget."""
    tool_attempt = 0
    filter_retried = False
    collection_limit = (CONFIG.get("max_collect_bytes", 0) if max_collect_bytes is None
                        else max_collect_bytes)
    while True:
        accumulator = ChatSSEAccumulator(max_collect_bytes=collection_limit)
        rejection = None
        async with _checked_chat_stream(url, headers, body, rid=rid, model_name=model_name,
                                        cred=cred) as response:
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
        if not detector.detected:
            allowed = dsml.tool_name_allowlist(body)
            converted, seen, _rejected = dsml.repair_aggregated(result, allowed)
            if converted:
                observe_attempt("dsml_repaired", calls=converted, blocks=seen, known=len(allowed))
                _log(f"[{rid}] 🔧 从正文还原 {converted} 个原生工具调用 (known={len(allowed)})")
                calls = result["choices"][0]["message"].get("tool_calls")
            elif seen:
                observe_attempt("dsml_rejected", blocks=seen, known=len(allowed))
                _log(f"[{rid}] ⚠ 识别到 {seen} 个原生标记块但未还原 (known={len(allowed)})")
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

async def _chat_sse_lines(url, headers, body, model_name, t0, rid, cred=None, *,
                          policy=None, tracker=None, state=None):
    """Yield validated Chat SSE with bounded filter detection and no streaming filter retries."""
    policy = policy or _snapshot_stream_policy("chat", body)
    if policy.aggregate:
        result = await _fetch_checked_chat(
            url, headers, body, model_name, rid, cred,
            max_collect_bytes=policy.max_collect_bytes)
        async for line in _replay_merged_sse(_completion_to_merged(result)):
            yield line
        _log_finish(model_name, t0, result, rid)
        return
    if tracker is None:
        tracker = ChatSSEAccumulator(
            collect=False, retain_tools=policy.realtime,
            budget=StreamOutputBudget(policy.max_collect_bytes))
    if state is not None:
        state["tracker"] = tracker

    def completed():
        merged = tracker.result(allow_empty_filter=policy.realtime)
        if policy.realtime:
            _validate_realtime_tools(
                merged.get("tool_calls"), body, merged.get("finish_reason"),
                filtered=tracker.filter_detector.detected)
        if state is not None:
            state["merged"] = merged
        return merged

    preview = bytearray()
    # Realtime output is never copied into the retired raw-text preview log.
    budget = 0 if policy.realtime else (
        CONFIG["log_body_limit"] if CONFIG.get("log_path") else 0)
    repairer = dsml.SSERepairer(dsml.tool_name_allowlist(body))
    try:
        async with _checked_chat_stream(url, headers, body, rid=rid, model_name=model_name,
                                        cred=cred) as response:
            async for line in response.aiter_lines():
                broken = False
                for out_line in repairer.feed(line):
                    tracker.feed_line(out_line)
                    if tracker.done or tracker.finish_reason:
                        completed()  # Never expose a success marker before terminal validation.
                    remaining = budget - len(preview)
                    if remaining > 0:
                        preview.extend((out_line[:remaining] + "\n").encode("utf-8")[:remaining])
                    yield out_line
                    if tracker.done:
                        yield ""
                        broken = True
                if broken:
                    break
        for out_line in repairer.flush():   # 上游未发 DONE 就断流时，残留字节同样交还
            if not (tracker.done or tracker.finish_reason):
                tracker.feed_line(out_line)
            yield out_line
        merged = completed()
    finally:
        if tracker.usage:
            observe_usage(tracker.usage)
    if tracker.filter_detector.detected:
        _note_content_filter(rid, model_name, final=True)
        return
    _note_dsml_observation(rid, repairer)
    _log(f"[{rid}] ◀ RESPONSE {model_name} | {time.time() - t0:.1f}s | stream finish={merged['finish_reason']}"
         + f" | tokens={(merged['usage'] or {}).get('total_tokens', '?')}")
    _log_text_body(f"[{rid}] RESPONSE SSE PREVIEW", preview.decode("utf-8", "replace"))


async def _stream_upstream(url: str, headers: dict, body: dict,
                           model_name: str = "?", t0: float = 0.0, rid: str = "", cred=None):
    policy = body.pop(_REQUEST_POLICY_KEY, None) or _snapshot_stream_policy("chat", body)
    sent = False
    # Realtime delivers every reasoning delta as it arrives; only compatible keeps the
    # historical single-block replay that hides the reasoning timeline from clients.
    if body.get("_server_tools"):
        # Gateway-executed web tools run as one aggregated loop, then replay.
        merged, _records = await _run_server_tool_merged(
            url, headers, body, model_name, t0, rid, cred, policy=policy)
        upstream = _replay_merged_sse(merged)
    else:
        upstream = _chat_sse_lines(url, headers, body, model_name, t0, rid, cred, policy=policy)
        if not policy.realtime:
            upstream = _coalesce_reasoning_sse(upstream, max_bytes=policy.max_collect_bytes)
    try:
        try:
            async for line in upstream:
                sent = True
                yield (_public_sse_line(line, model_name) + "\n").encode("utf-8")
        except (httpx.HTTPError, UpstreamResponseError) as error:
            if not sent:
                raise      # Preserve the HTTP error while no response bytes have been sent.
            status, raw = _upstream_failure(error, model_name, t0, rid)
            yield _err_event(raw, status)
    finally:
        # The inner generator may be suspended at a yielded line when its
        # consumer reports an adapter error or a downstream disconnect.
        await _close_stream(upstream)




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
        if error.status in FAILOVER_CODES:
            return True
        if error.status == 400 and is_channel_check_error(raw):
            return True
        return (error.from_channel_fallback
                and _parse_not_servable(raw, error.status) is not None)
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


def _wrap_agenttool_error(status: int, raw: bytes, error) -> HTTPException:
    """Re-shape upstream or transport failures as gateway error envelopes for agenttool consumers."""
    detail = _safe_err_raw(raw, status)
    err = detail.get("error") if isinstance(detail, dict) else None
    if not (isinstance(detail, dict) and isinstance(err, dict) and err.get("message")):
        message = (err.get("message") if isinstance(err, dict) else None) or (
            detail.get("message") if isinstance(detail, dict) else None) or (
            detail.get("msg") if isinstance(detail, dict) else None) or (
            raw.decode("utf-8", "replace").strip()[:500] or f"upstream HTTP {status}")
        etype = ("rate_limit_error" if status == 429
                 else "invalid_request_error" if 400 <= status < 500 else "upstream_error")
        detail = {"error": {"message": message, "type": etype, "code": status}}
    return HTTPException(status_code=status, detail=detail,
                         headers=error.headers if isinstance(error, UpstreamHTTPError) else None)


@dataclass(frozen=True)
class _AgentTool:
    """One upstream agenttool endpoint: where it lives, how its reply is read and how it is reported."""
    name: str                       # Route model and log tag, e.g. "web-search".
    url_for: Callable[[dict], str]
    timeout: float
    parse: Callable[[Any], Any]     # Raises ValueError on an unusable body.
    invalid_body: str               # Client-facing 502 text for a non-JSON body.
    bad_response: str               # Failure code recorded for an unusable body.
    summarize: Callable[[Any], str]


_WEB_SEARCH = _AgentTool(
    name="web-search", url_for=search_url_for_headers, timeout=web_search.QUERY_TIMEOUT,
    parse=web_search.parse_results, invalid_body="搜索响应体不是有效的 JSON",
    bad_response="bad_search_response", summarize=lambda results: f"{len(results)} 条")
_WEB_FETCH = _AgentTool(
    name="web-fetch", url_for=fetch_url_for_headers, timeout=web_fetch.FETCH_TIMEOUT,
    parse=web_fetch.parse_content, invalid_body="网页抓取响应体不是有效的 JSON",
    bad_response="bad_webfetch_response",
    summarize=lambda content: f"{len(content.get('content') or '')} 字符")


async def _agenttool_call(tool: _AgentTool, body: dict, rid: str, *, borrowed=None):
    """POST one agenttool request with bounded credential failover; returns the parsed reply.

    ``borrowed`` is a ``(cred, headers)`` lease the caller keeps owning: it serves the first
    attempt and is never released here. Failover then leases other accounts outside the session
    binding, so a conversation that lent its account never moves to another one.
    """
    label = tool.name.replace("-", " ").upper()
    tried = []
    recovered = None
    while True:
        owned = borrowed is None or bool(tried)
        if owned:
            cred, headers = await run_in_threadpool(lambda: _cred_for(
                {"model": tool.name}, None, tried=tried, sticky=borrowed is None))
        else:
            cred, headers = borrowed
        url = tool.url_for(headers)
        profile = profile_for_headers(headers)
        observe_route(public_model=tool.name, upstream_model=None, profile=profile,
                      credential=account_key(profile, headers.get("X-User-Id"), headers.get("X-Enterprise-Id")))
        t0 = time.time()
        try:
            async with _backend_stream(url, headers, body, timeout=tool.timeout,
                                       rid=rid, model_name=tool.name) as response:
                if response.status_code != 200:
                    _check_upstream_status(response.status_code, await read_bounded_error(response),
                                           cred, None, headers=response.headers)
                raw = await response.aread()
            try:
                payload = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                raise ValueError(tool.invalid_body) from None
            result = tool.parse(payload)
        except (httpx.HTTPError, UpstreamResponseError) as error:
            status, raw = _upstream_failure(error, tool.name, t0, rid)
            recovered = observe_failure_seq()
            tried.append(_cred_manager(cred))
            limit = _failover_limit()
            if limit <= 0 or len(tried) > limit or not _failover_safe(error, raw):
                raise _wrap_agenttool_error(status, raw, error) from None
            _log(f"[{rid}] ↻ 换凭证重放 {len(tried)}/{limit} | {tool.name} | 上游 HTTP {status}"
                 f"{_replay_cost_note(error)}")
            continue
        except ValueError as error:
            observe_failure(tool.bad_response)
            raise HTTPException(status_code=502, detail={"error": {"message": str(error),
                                             "type": "upstream_error"}}) from None
        finally:
            if owned:
                release_credential(cred)
        _note_cred_model_ok(cred, None)
        if tried:
            observe_recovery(recovered)
        _log(f"[{rid}] ✔ {label} {tool.summarize(result)} | {profile} | {time.time() - t0:.1f}s")
        return result


async def _web_search(query: str, max_results: int, allowed_domains=None, freshness: str = "",
                      *, borrowed=None) -> list:
    """Run one upstream web search with bounded credential failover; returns parsed results."""
    rid = _request_id()
    _log(f"[{rid}] ▶ WEB SEARCH | chars={len(query)} | max_results={max_results}")
    body = web_search.request_body(query, max_results, allowed_domains=allowed_domains,
                                    freshness=freshness)
    return await _agenttool_call(_WEB_SEARCH, body, rid, borrowed=borrowed)


async def _web_fetch(url: str, prompt: str = "", max_length: int | None = None, *,
                     borrowed=None) -> dict:
    """Run one upstream web fetch with bounded credential failover; returns parsed content."""
    rid = _request_id()
    _log(f"[{rid}] ▶ WEB FETCH | chars={len(url)}")
    body = web_fetch.request_body(url, prompt, max_length or web_fetch.DEFAULT_MAX_LENGTH)
    return await _agenttool_call(_WEB_FETCH, body, rid, borrowed=borrowed)


# ---------------------------------------------------------------------------
# Media generation (images and videos)
# ---------------------------------------------------------------------------

MEDIA_IMAGE_TIMEOUT = 180
MEDIA_VIDEO_SUBMIT_TIMEOUT = 60
MEDIA_VIDEO_QUERY_TIMEOUT = 30
MEDIA_DOWNLOAD_TIMEOUT = 120
_MEDIA_ID = re.compile(r"(img|vid)_[0-9a-f]{32}\Z")
_MEDIA_PUBLIC_KINDS = {"image": "images", "video": "videos"}


class MediaDownloadError(RuntimeError):
    """A deterministic download failure: bad signature, refused type or over quota."""


class MediaDownloadRetryable(RuntimeError):
    """A transient download failure; the next query may retry the same resource."""


# Per-task refresh locks: one concurrent upstream query and download per task id.
_MEDIA_TASK_LOCKS: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()


def _media_task_lock(task_id: str) -> asyncio.Lock:
    lock = _MEDIA_TASK_LOCKS.get(task_id)
    if lock is None:
        lock = asyncio.Lock()
        _MEDIA_TASK_LOCKS[task_id] = lock
    return lock

@dataclass(frozen=True)
class _MediaAction:
    """One upstream media endpoint: where it lives and how long it may take."""
    name: str
    path: str
    timeout: float


MEDIA_IMAGE_GENERATE = _MediaAction("image-generate", "/v2/images/generations", MEDIA_IMAGE_TIMEOUT)
MEDIA_IMAGE_EDIT = _MediaAction("image-edit", "/v2/images/edits", MEDIA_IMAGE_TIMEOUT)
MEDIA_VIDEO_SUBMIT = _MediaAction("video-submit", "/v2/videos/generations", MEDIA_VIDEO_SUBMIT_TIMEOUT)
MEDIA_VIDEO_QUERY = _MediaAction("video-query", "/v2/videos/tasks", MEDIA_VIDEO_QUERY_TIMEOUT)


def _media_store():
    """Return the media store or refuse media endpoints with 503."""
    store = CONFIG.get("media_store")
    if store is None:
        raise HTTPException(status_code=503, detail={"error": {
            "message": "媒体存储不可用，请检查数据目录权限或重启网关", "type": "service_unavailable"}})
    return store


def _media_base_url() -> str:
    return str(CONFIG.get("media_public_base_url") or "").strip().rstrip("/")


def _media_ttl_hours() -> float:
    try:
        return float(CONFIG.get("media_ttl_hours") or 72)
    except (TypeError, ValueError):
        return 72.0


def _media_max_bytes() -> int:
    try:
        return max(64, int(CONFIG.get("media_max_total_mb") or 1024)) * 1024 * 1024
    except (TypeError, ValueError):
        return 1024 * 1024 * 1024


def _media_public_url(meta: dict) -> str:
    return f"{_media_base_url()}/v1/media/{_MEDIA_PUBLIC_KINDS[meta['kind']]}/{meta['id']}"


def _media_dynamic_kinds(model) -> list:
    """Media kinds declared for this model in account root tables (serves scope)."""
    index = _catalog_index()
    if index is not None:
        return list(index.media_kinds.get(model, ()))
    kinds: list = []  # No index yet: scan the published catalogs once.
    for account in (CONFIG.get("account_catalogs") or {}).values():
        for item in ((account or {}).get("serves") or []) if isinstance(account, dict) else []:
            if isinstance(item, dict) and item.get("id") == model:
                kinds.extend(kind for kind in media_generation.catalog_kinds(item) if kind not in kinds)
    return kinds


def _media_all_kinds(model) -> list:
    kinds = list(media_generation.model_kinds(model))
    kinds.extend(kind for kind in _media_dynamic_kinds(model) if kind not in kinds)
    return kinds


_MEDIA_ENDPOINT_HINT = "媒体生成请使用 /v1/images/generations、/v1/images/edits 或 /v1/videos 接口"


def _media_chat_guard(model) -> None:
    """Keep media models out of chat routing with one shared check."""
    if model and (media_generation.model_kinds(model) or _media_dynamic_kinds(model)):
        raise HTTPException(status_code=400, detail={"error": {
            "message": f"模型 {model} 是媒体生成模型，不走聊天接口；{_MEDIA_ENDPOINT_HINT}",
            "type": "invalid_request_error", "code": "media_model_not_chat"}})


def _media_check_model(model, kind: str) -> None:
    """Accept only models in the static ∪ dynamic validation set for this endpoint kind."""
    if not isinstance(model, str) or kind not in _media_all_kinds(model):
        raise HTTPException(status_code=400, detail={"error": {
            "message": f"模型 {model} 不在媒体生成校验集内；{_MEDIA_ENDPOINT_HINT}",
            "type": "invalid_request_error", "code": "model_not_found", "param": "model"}})


def _media_account_serves(account, model) -> bool:
    """Account catalog membership with a static-list fallback for unsynced accounts."""
    items = account.get("serves") if isinstance(account, dict) else None
    if items is None:
        return bool(media_generation.model_kinds(model))
    return any(isinstance(item, dict) and item.get("id") == model for item in items)


def _media_cred_for(model, tried=()):
    """Lease one account allowed to serve paid media; mirror pool health and capacity."""
    pool = CONFIG.get("cred_pool")
    if pool is None:
        cm = CONFIG.get("cred")
        if cm is None or cm in {_cred_manager(item) for item in tried}:
            raise HTTPException(status_code=503, detail={"error": {
                "message": "未找到可用的媒体生成凭证，请先在桌面端登录 CodeBuddy", "type": "auth_error"}})
        with cm._lock:
            headers = cm.get_headers()
        if profile_for_headers(headers) not in media_generation.MEDIA_PROFILES:
            raise HTTPException(status_code=503, detail={"error": {
                "message": "当前凭证产品不支持媒体生成", "type": "auth_error"}})
        return cm, headers
    policy = model_policy.view(CONFIG)
    accounts = CONFIG.get("account_catalogs") or {}
    excluded = {_cred_manager(item) for item in tried}
    with pool._lock:
        candidates = []
        for entry in pool._entries:
            profile = CredentialPool._entry_profile(entry)
            if (entry["cm"] in excluded or not profile
                    or profile not in media_generation.MEDIA_PROFILES
                    or not pool._healthy(entry, policy)
                    or not _media_account_serves(accounts.get(entry.get("account_key")), model)
                    or not pool._has_credit(entry, profile)):
                continue
            candidates.append(entry)
        candidates.sort(key=pool._expiry_rank)
    if not candidates:
        raise HTTPException(status_code=503, detail={"error": {
            "message": "没有符合媒体路由条件的账号（需支持媒体的产品、目录包含该模型且有余额）",
            "type": "service_unavailable", "code": "no_media_account"}})
    busy = False
    for entry in candidates:
        cm = entry["cm"]
        try:
            with cm._lock:
                headers = cm.get_headers()
                generation = cm._generation
        except Exception as error:
            pool.cooldown(cm, reason=str(error), generation=cm._generation)
            continue
        if profile_for_headers(headers) not in media_generation.MEDIA_PROFILES:
            continue
        lease = pool._capacity.acquire(pool._capacity_key(entry),
                                       CONFIG.get("max_inflight_per_account", 0), cm, generation)
        if lease is None:
            busy = True
            continue
        resources = request_resources.get()
        if resources is not None:
            resources.add(lease)
        return lease, headers
    if busy:
        raise HTTPException(status_code=503, headers={"Retry-After": "3"}, detail={"error": {
            "message": "媒体生成账号在途名额已满，请稍后重试", "type": "service_unavailable",
            "code": "credential_concurrency_limit"}})
    raise HTTPException(status_code=503, detail={"error": {
        "message": "媒体生成账号暂时不可用（令牌刷新失败或产品不符），请稍后重试",
        "type": "service_unavailable"}})


def _media_bound_cred(account_identity: str):
    """Lease the exact account a task was submitted with, ignoring health gates."""
    pool = CONFIG.get("cred_pool")
    if pool is None:
        cm = CONFIG.get("cred")
        if cm is None:
            raise HTTPException(status_code=503, detail={"error": {
                "message": "任务绑定的凭证不存在，无法查询", "type": "auth_error"}})
        with cm._lock:
            return cm, cm.get_headers()
    with pool._lock:
        entry = next((e for e in pool._entries if e.get("account_key") == account_identity), None)
    if entry is None:
        raise HTTPException(status_code=409, detail={"error": {
            "message": "提交该任务的账号已移出凭证池，无法查询；请重新提交任务",
            "type": "invalid_request_error", "code": "account_removed"}})
    with entry["cm"]._lock:
        return entry["cm"], entry["cm"].get_headers()


def _media_replayable(error) -> bool:
    """Replay only when the body was never accepted or upstream explicitly refused."""
    if isinstance(error, UpstreamHTTPError):
        return error.status in (401, 403, 429)
    return isinstance(error, REPLAYABLE_TRANSPORT)


async def _media_call(action: _MediaAction, body: dict, rid: str, *, model, parse,
                      cred=None, headers=None, allow_failover=True, lease_note=None):
    """POST one media request and parse the reply; paid media never replays timeouts or 5xx."""
    tried = []
    recovered = None
    owned = cred is None
    while True:
        if owned and cred is None:
            cred, headers = await run_in_threadpool(_media_cred_for, model, tried)
        profile = profile_for_headers(headers)
        url = media_url_for_headers(headers, action.path)
        observe_route(public_model=model, upstream_model=model, profile=profile,
                      credential=account_key(profile, headers.get("X-User-Id"), headers.get("X-Enterprise-Id")))
        t0 = time.time()
        released = False
        try:
            async with _backend_stream(url, headers, body, timeout=action.timeout,
                                       rid=rid, model_name=action.name) as response:
                if response.status_code != 200:
                    raw = await read_bounded_error(response)
                    code, message, _ = _upstream_diagnostic(raw)
                    observe_attempt("upstream_error", status_code=response.status_code,
                                    upstream_code=code, upstream_msg=message)
                    # Health isolation: only 401 reaches the cooldown/refresh path.
                    if response.status_code == 401:
                        _note_cred_status(cred, 401, raw=raw)
                    raise UpstreamHTTPError(
                        response.status_code, raw,
                        retry_after=parse_retry_after(response.headers.get("Retry-After")))
                raw = await response.aread()
            try:
                result = parse(json.loads(raw.decode("utf-8", "replace")))
            except media_generation.MediaUpstreamError as error:
                # A business refusal may already be billed; never replay it on another account.
                observe_failure("media_upstream_refused")
                raise HTTPException(status_code=502, detail={"error": {
                    "message": f"上游媒体接口返回错误 {error.upstream_code}：{error.message}",
                    "type": "upstream_error", "upstream_code": error.upstream_code}}) from None
            except ValueError as error:
                observe_failure("bad_media_response")
                raise HTTPException(status_code=502, detail={"error": {
                    "message": str(error), "type": "upstream_error"}}) from None
        except (httpx.HTTPError, UpstreamResponseError) as error:
            status, raw = _upstream_failure(error, action.name, t0, rid)
            recovered = observe_failure_seq()
            tried.append(cred)
            limit = _failover_limit()
            surface = _wrap_agenttool_error(status, raw, error)
            if not allow_failover or limit <= 0 or len(tried) > limit or not _media_replayable(error):
                raise surface from None
            _log(f"[{rid}] ↻ 媒体换号重放 {len(tried)}/{limit} | {action.name} | 上游 HTTP {status}")
            release_credential(cred)
            released = True
            try:
                cred, headers = await run_in_threadpool(_media_cred_for, model, tried)
            except HTTPException:
                # No replacement account: report the original upstream refusal.
                raise surface from None
            owned = True
            continue
        finally:
            if owned and not released:
                release_credential(cred)
        if tried:
            observe_recovery(recovered)
        if lease_note is not None:
            lease_note["profile"] = profile
            lease_note["credential"] = account_key(profile, headers.get("X-User-Id"),
                                                   headers.get("X-Enterprise-Id"))
        _log(f"[{rid}] ✔ {action.name.upper()} | {profile} | {model} | {time.time() - t0:.1f}s")
        return result


async def _media_download_bytes(url: str, rid: str) -> bytes:
    """Fetch one upstream resource into memory, bounded by the configured media quota."""
    limit = _media_max_bytes()
    data = bytearray()
    async with httpx.AsyncClient(timeout=MEDIA_DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
        try:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    if response.status_code >= 500:
                        raise MediaDownloadRetryable(f"资源下载失败（HTTP {response.status_code}）")
                    detail = "，签名可能已过期" if response.status_code in (403, 404) else ""
                    raise MediaDownloadError(
                        f"资源下载失败（HTTP {response.status_code}{detail}）")
                async for chunk in response.aiter_bytes(64 * 1024):
                    data.extend(chunk)
                    if len(data) > limit:
                        raise MediaDownloadError("单个资源超过媒体容量上限")
        except httpx.HTTPError as error:
            raise MediaDownloadRetryable(f"资源下载失败：{type(error).__name__}") from None
    return bytes(data)


async def _media_save(kind: str, data: bytes, *, model, rid: str) -> dict:
    """Persist downloaded bytes through the media store's sniffing and eviction."""
    if len(data) > _media_max_bytes():
        raise MediaDownloadError("单个资源超过媒体容量上限")
    try:
        return await run_in_threadpool(_media_store().save_asset, kind, data, model=model,
                                       ttl_hours=_media_ttl_hours(),
                                       max_total_bytes=_media_max_bytes())
    except ValueError:
        raise
    except Exception as error:
        raise MediaDownloadError(f"媒体存储写入失败：{type(error).__name__}") from None


async def _media_video_refresh(task_id: str, rid: str) -> dict:
    """Query one upstream video task on its bound account and persist the result.

    One refresh lock per task keeps concurrent queries from downloading twice; the
    task row is re-read under the lock so only the winner talks to the upstream.
    """
    store = _media_store()

    async def load():
        return await run_in_threadpool(store.get_task, task_id)

    task = await load()
    if task is None:
        raise HTTPException(status_code=404, detail={"error": {
            "message": "视频任务不存在或已过期", "type": "not_found_error", "code": "task_not_found"}})
    if task["status"] in ("completed", "failed"):
        return task  # Repeated queries keep returning the same resource or error.
    async with _media_task_lock(task_id):
        task = await load()
        if task is None:
            raise HTTPException(status_code=404, detail={"error": {
                "message": "视频任务不存在或已过期", "type": "not_found_error", "code": "task_not_found"}})
        if task["status"] in ("completed", "failed"):
            return task
        cred, headers = await run_in_threadpool(_media_bound_cred, task["account_key"])
        status, urls, credit, message = await _media_call(
            MEDIA_VIDEO_QUERY, {"task_id": task_id}, rid, model=task["model"],
            parse=media_generation.parse_video_task, cred=cred, headers=headers, allow_failover=False)
        media_id = None
        if status == "failed":
            await run_in_threadpool(store.update_task, task_id, status="failed",
                                    error=message or "视频生成失败", credit=credit)
        elif status == "completed":
            media_id = task["media_id"]
            if media_id is None and not urls:
                # The upstream finished without a resource: nothing to retry.
                await run_in_threadpool(store.update_task, task_id, status="failed",
                                        error="上游未返回视频地址", credit=credit)
            elif media_id is None:
                try:
                    meta = await _media_save("video", await _media_download_bytes(urls[0], rid),
                                             model=task["model"], rid=rid)
                    media_id = meta["id"]
                except MediaDownloadError as error:   # Deterministic: signature gone or quota.
                    _log(f"[{rid}] ✗ 视频资源下载失败 | {task_id} | {error}")
                    await run_in_threadpool(store.update_task, task_id, status="failed",
                                            error=f"视频资源下载失败：{error}", credit=credit)
                except ValueError:                    # Sniffer rejected the payload (#8): terminal.
                    await run_in_threadpool(store.update_task, task_id, status="failed",
                                            error="资源类型不在允许的白名单内", credit=credit)
                except MediaDownloadRetryable as error:
                    # Transient network trouble: stay in progress and retry on the next query.
                    _log(f"[{rid}] ↻ 视频资源下载暂缓 | {task_id} | {error}")
                    await run_in_threadpool(store.update_task, task_id,
                                            error=f"资源下载暂缓，下次查询重试：{error}", credit=credit)
        if status == "completed" and media_id is not None:
            await run_in_threadpool(store.update_task, task_id, status="completed",
                                    media_id=media_id, error=None, credit=credit)
        elif status not in ("completed", "failed"):
            await run_in_threadpool(store.update_task, task_id, status=status, credit=credit)
    return await load()


def _media_video_response(task: dict, meta) -> dict:
    """Render one task row in the public video shape; meta is the pre-fetched asset."""
    body = {"id": task["task_id"], "object": "video", "status": task["status"],
            "model": task["model"], "created_at": int(task["created"])}
    if task["status"] == "completed":
        expired = meta is not None and media_store.is_expired(meta, _media_ttl_hours())
        if meta is not None and not expired and _media_base_url():
            body["video"] = {"url": _media_public_url(meta)}
        elif meta is not None and not expired:
            body["message"] = "视频已保存到网关本地；设置资源访问地址（media_public_base_url）后可获取链接"
        elif task["error"]:
            body["message"] = task["error"]
        else:
            body["message"] = "视频资源已删除或过期，不再重新下载"
    elif task["status"] == "failed":
        body["error"] = {"message": task["error"] or "视频生成失败"}
    else:
        body["message"] = task["error"]  # A retryable download hint, if any.
    if task["credit"] is not None:
        body["usage"] = {"credit": task["credit"]}
    return body


def _media_bad_request(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail={"error": {
        "message": message, "type": "invalid_request_error"}})


async def _media_json(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception as e:
        raise _bad_json(e) from None
    if not isinstance(payload, dict):
        raise _media_bad_request("请求体必须是 JSON 对象")
    return payload


def _media_optional_text(value, field: str, limit: int = media_generation.MAX_PROMPT_CHARS):
    """Accept an optional public string field; None passes through untouched."""
    if value is None:
        return None
    if (not isinstance(value, str) or len(value) > limit
            or any(ord(c) < 32 and c not in "\n\r\t" for c in value)):
        raise _media_bad_request(f"{field} 必须是不超过 {limit} 字符的字符串")
    return value


async def _media_image_item(item: dict, model, rid: str, *, persist: bool):
    """Convert one upstream item; returns (payload, persisted_id, error)."""
    revised = {"revised_prompt": item["revised_prompt"]} if item.get("revised_prompt") else {}
    try:
        if item.get("b64_json"):
            if persist:
                meta = await _media_save("image", base64.b64decode(item["b64_json"]),
                                         model=model, rid=rid)
                return {"url": _media_public_url(meta), **revised}, meta["id"], None
            return {"b64_json": item["b64_json"], **revised}, None, None
        data = await _media_download_bytes(item["url"], rid)
        if persist:
            meta = await _media_save("image", data, model=model, rid=rid)
            return {"url": _media_public_url(meta), **revised}, meta["id"], None
        return {"b64_json": base64.b64encode(data).decode("ascii"), **revised}, None, None
    except (MediaDownloadError, MediaDownloadRetryable, ValueError) as error:
        return None, None, str(error)


async def _media_image_reply(items: list, model, rid: str, *, persist: bool) -> list:
    """Download all image items concurrently; keep successes and never orphan files."""
    results = await asyncio.gather(*(_media_image_item(item, model, rid, persist=persist)
                                     for item in items[:media_generation.MAX_IMAGE_ITEMS]))
    out, persisted, failures, first_error = [], [], 0, ""
    for payload, asset_id, error in results:
        if error is None:
            out.append(payload)
            if asset_id:
                persisted.append(asset_id)
        else:
            failures += 1
            first_error = first_error or error
    if failures == len(items):
        store = CONFIG.get("media_store")
        if store is not None and persisted:
            await run_in_threadpool(
                lambda: [store.delete_asset(asset_id) for asset_id in persisted])
        raise HTTPException(status_code=502, detail={"error": {
            "message": f"全部 {len(items)} 张图片下载或保存失败：{first_error}",
            "type": "upstream_error"}})
    if failures:
        _log(f"[{rid}] ✗ 图片部分失败 {failures}/{len(items)} | {first_error}")
        observe_failure("image_item_failed")
    return out


def _media_model_accounts(model) -> int:
    """Count accounts the media routing rules would currently select for this model."""
    pool = CONFIG.get("cred_pool")
    if pool is None:
        return 1 if CONFIG.get("cred") is not None else 0
    policy = model_policy.view(CONFIG)
    accounts = CONFIG.get("account_catalogs") or {}
    with pool._lock:
        count = 0
        for entry in pool._entries:
            profile = CredentialPool._entry_profile(entry)
            if (profile and profile in media_generation.MEDIA_PROFILES
                    and model_policy.credential_enabled(CONFIG, entry, policy)
                    and _media_account_serves(accounts.get(entry.get("account_key")), model)
                    and pool._has_credit(entry, profile)):
                count += 1
        return count


def admin_media_models() -> list:
    """Model table for the media page: static families plus catalog-tagged entries."""
    defaults = {"image": media_generation.DEFAULT_IMAGE_MODEL,
                "edit": media_generation.DEFAULT_IMAGE_EDIT_MODEL,
                "video": media_generation.DEFAULT_VIDEO_MODEL}
    static_kinds: dict = {}
    for kind, models in (("image", media_generation.IMAGE_TEXT_MODELS),
                         ("edit", media_generation.IMAGE_EDIT_MODELS),
                         ("video", media_generation.VIDEO_MODELS)):
        for model in models:
            static_kinds.setdefault(model, []).append(kind)
    catalog_kinds_map = _catalog_index().media_kinds if _catalog_index() is not None else None
    if catalog_kinds_map is None:
        catalog_kinds_map = {}  # No index yet: derive kinds from the published serves scan.
        for account in (CONFIG.get("account_catalogs") or {}).values():
            for item in ((account or {}).get("serves") or []) if isinstance(account, dict) else []:
                if (isinstance(item, dict) and isinstance(item.get("id"), str)
                    and media_generation.catalog_kinds(item)):
                    known = catalog_kinds_map.setdefault(item["id"], ())
                    catalog_kinds_map[item["id"]] = tuple(
                        dict.fromkeys(known + media_generation.catalog_kinds(item)))
    table = {model: {"kinds": tuple(kinds), "catalog": False}
             for model, kinds in static_kinds.items()}
    for model, kinds in catalog_kinds_map.items():
        if not kinds:
            continue  # Only tagged media entries join the table.
        entry = table.setdefault(model, {"kinds": (), "catalog": False})
        entry["catalog"] = True
        entry["kinds"] = tuple(dict.fromkeys(entry["kinds"] + kinds))
    return [{"id": model, "kinds": list(entry["kinds"]),
             "sources": (["static"] if model in static_kinds else [])
                        + (["catalog"] if entry["catalog"] else []),
             "default": [kind for kind, name in defaults.items() if name == model],
             "accounts": _media_model_accounts(model)}
            for model, entry in sorted(table.items())]


def _mcp_tool(call, failure: str):
    """Adapt a gateway web tool for the MCP layer; map gateway errors to tool text instead of HTTP failures."""
    async def run(*args):
        try:
            return await call(*args), None
        except HTTPException as error:
            detail = error.detail.get("error", {}) if isinstance(error.detail, dict) else {}
            message = str(detail.get("message") or error.detail or failure)
            code = detail.get("code") if isinstance(detail, dict) else None
            observe_failure(str(code) if code else f"http_{error.status_code}")
            return None, message
    return run


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
        raise _bad_json(e) from None

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
    projection_mode = CONFIG.get("responses_projection_mode", "balanced")
    projection_max_bytes = int(CONFIG.get("responses_projection_max_bytes", 40000))
    try:
        chat_body, projection_stats = await run_in_threadpool(
            project_responses_chat_body, chat_body, mode=projection_mode,
            max_item_bytes=projection_max_bytes)
    except UnicodeError:
        raise HTTPException(status_code=400, detail={"error": {
            "message": "request contains text that cannot be encoded as UTF-8",
            "type": "invalid_request_error", "param": "input",
            "code": "invalid_unicode"}}) from None
    observe_responses_projection(projection_stats)
    chat_body = await run_in_threadpool(_prepare_chat_body, chat_body)

    client_wants_stream = _client_wants_stream(payload)
    stream_policy = _snapshot_stream_policy("responses", chat_body)
    observe_stream_mode(stream_policy.mode)
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
        f"| harness_messages={projection_stats.get('harness_messages_projected', 0)} "
        f"| truncated_items={projection_stats.get('truncated_items', 0)} "
        f"| truncated_bytes={projection_stats.get('truncated_original_bytes', 0)}→"
        f"{projection_stats.get('truncated_projected_bytes', 0)}"
    )
    # Keep blocking credential selection and refresh off the event loop.
    prepared = chat_body        # Preserve canonical input for routing policy checks.
    chat_body, cred, headers, url = await run_in_threadpool(_route_chat, payload, chat_body, rid)
    _log_json(f"[{rid}] RESPONSES → CHAT BODY (预览)", chat_body)
    t0 = time.time()

    if client_wants_stream:
        def attempt(routed, cred, headers, url):
            return _stream_responses(url, headers, _body_with_stream_policy(routed, stream_policy),
                                     model_name, t0, rid, cred=cred)
        response = _routed_stream(payload, prepared, model_name, rid, t0, attempt,
                                    chat_body, cred, headers, url)
        response.headers["X-CodeBuddy-Responses-Projection"] = projection_mode
        return response

    response = await _nonstream_adapted(
        url, headers, chat_body, model_name, t0, rid, cred, payload=payload, canonical=prepared,
        request=request, policy=stream_policy)
    response.headers["X-CodeBuddy-Responses-Projection"] = projection_mode
    return response


async def _run_server_tool_chat(url, headers, routed, model_name, rid, cred, *, policy):
    """Fetch one Chat completion, running declared gateway web tools to completion."""
    spec = routed.get("_server_tools") or {}
    continuing = False

    async def fetch_chat(tool_body):
        nonlocal continuing
        if continuing:
            # Continuations grow the body with fetched content. The first round was already
            # measured by _prepare_chat_body and _route_chat.
            await run_in_threadpool(_guard_request_size, tool_body)
        continuing = True
        return await _fetch_checked_chat(url, headers, tool_body, model_name, rid, cred,
                                         filter_retry=True,
                                         max_collect_bytes=policy.max_collect_bytes)
    if not spec:
        return await fetch_chat(routed), []
    _log(f"[{rid}] ⚙ 内置联网工具已注入 | {sorted(spec)}")
    # Tool calls are not requests of their own: they fold into this request's audit record,
    # and they run on this attempt's account instead of leasing capacity or sticky binding.
    borrowed = (cred, headers)

    async def search_fn(query, count, allowed, freshness):
        with observe_nested("web-search"):
            return await _web_search(query, count, allowed, freshness, borrowed=borrowed)

    async def fetch_fn(url, prompt, max_length):
        with observe_nested("web-fetch"):
            return await _web_fetch(url, prompt, max_length, borrowed=borrowed)

    result, records = await server_tools.run_chat_tool_loop(
        fetch_chat, routed, spec, search_fn=search_fn, fetch_fn=fetch_fn)
    if records:
        _log(f"[{rid}] ⚙ 内置联网工具执行 {len(records)} 次 | "
             f"{[record['kind'] for record in records]}")
        # Each round reported its own usage; the audit record holds the total.
        observe_usage(result.get("usage") or {})
    return result, records


async def _run_server_tool_merged(url, headers, routed, model_name, t0, rid, cred, *, policy):
    """Run gateway web tools for a streaming client; returns the final merged completion and records."""
    result, records = await _run_server_tool_chat(
        url, headers, routed, model_name, rid, cred, policy=policy)
    _log_finish(model_name, t0, result, rid)
    return _completion_to_merged(result), records


async def _nonstream_adapted(url, headers, body, model_name, t0, rid, cred, *, anthropic=False,
                             payload=None, canonical=None, request=None, policy=None):
    policy = policy or _snapshot_stream_policy("messages" if anthropic else "responses", body)
    tool_registry = body.get("_tool_registry")
    converter = (AnthropicStreamConverter(model=model_name) if anthropic else
                 ResponsesStreamConverter(model=model_name,
                                          parallel_tool_calls=body.get("parallel_tool_calls", True),
                                          tool_registry=tool_registry))

    server_records: list = []

    async def fetch(routed, cred, headers, url):
        result, records = await _run_server_tool_chat(
            url, headers, routed, model_name, rid, cred, policy=policy)
        server_records.extend(records)
        return result
    try:
        collected = await await_or_hangup(
            _routed_fetch(payload, body if canonical is None else canonical,
                          model_name, rid, t0, fetch, body, cred, headers, url), request)
        if server_records:
            if anthropic:
                converter.seed_server_tools(server_records)
            else:
                converter.seed_web_search_calls(server_records)
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
    protocol = "messages" if anthropic else "responses"
    policy = body.pop(_REQUEST_POLICY_KEY, None) or _snapshot_stream_policy(protocol, body)
    tool_registry = body.get("_tool_registry")
    state = {}
    tracker = None
    declared_names = _declared_tool_names(body)
    # Gateway web tools replay one aggregated result, which the realtime accumulator never sees.
    realtime = policy.realtime and not body.get("_server_tools")
    if realtime:
        budget = StreamOutputBudget(policy.max_collect_bytes)
        tracker = ChatSSEAccumulator(collect=False, retain_tools=True, budget=budget,
                                     declared_names=declared_names)
        converter = (AnthropicStreamConverter(model=model_name, realtime=True, budget=budget,
                                              tool_states=tracker.tools, declared_names=declared_names)
                     if anthropic else
                     ResponsesStreamConverter(model=model_name,
                                              parallel_tool_calls=body.get("parallel_tool_calls", True),
                                              realtime=True, budget=budget, tool_states=tracker.tools,
                                              declared_names=declared_names,
                                              tool_registry=tool_registry))
    else:
        converter = (AnthropicStreamConverter(model=model_name) if anthropic else
                     ResponsesStreamConverter(model=model_name,
                                              parallel_tool_calls=body.get("parallel_tool_calls", True),
                                              tool_registry=tool_registry))
    sent = False
    if body.get("_server_tools"):
        # Gateway-executed web tools run as one aggregated loop, then replay.
        merged, records = await _run_server_tool_merged(
            url, headers, body, model_name, t0, rid, cred, policy=policy)
        if records:
            if anthropic:
                converter.seed_server_tools(records)
            else:
                converter.seed_web_search_calls(records)
        upstream = _replay_merged_sse(merged)
    else:
        upstream = _chat_sse_lines(url, headers, body, model_name, t0, rid, cred,
                                   policy=policy, tracker=tracker, state=state)
        if not realtime:
            upstream = _coalesce_reasoning_sse(upstream, max_bytes=policy.max_collect_bytes)
    try:
        try:
            async for line in upstream:
                events = converter.feed_line(_public_sse_line(line, model_name))
                if events:
                    sent = True
                    yield events.encode("utf-8")
            if realtime:
                converter.set_validated_tools((state.get("merged") or {}).get("tool_calls"))
                if tracker.filter_detector.detected:
                    converter.mark_content_filter()
            events = converter.finish()
            if events:
                sent = True
                yield events.encode("utf-8")
        except (httpx.HTTPError, UpstreamResponseError) as error:
            if not sent:
                raise      # Preserve the HTTP error before any response bytes have been sent.
            status, raw = _upstream_failure(error, model_name, t0, rid)
            event = {"type": "error", "error": {
                "message": sanitize_log_text(raw.decode("utf-8", "replace"), 512),
                "type": "api_error" if anthropic else "upstream_error", "code": status}}
            prefix = "event: error\n" if anthropic else ""
            yield (prefix + f"data: {json.dumps(event, ensure_ascii=False)}\n\n").encode("utf-8")
        except ValueError:
            # Adapter metadata/append-only checks are protocol failures, not
            # uncaught application errors after a response has opened.
            failure = UpstreamResponseError(502, b'{"error":{"message":"Invalid upstream tool_calls",'
                                                    b'"type":"upstream_error","code":"invalid_tool_calls"}}')
            if not sent:
                raise failure from None
            status, raw = _upstream_failure(failure, model_name, t0, rid)
            event = {"type": "error", "error": {
                "message": sanitize_log_text(raw.decode("utf-8", "replace"), 512),
                "type": "api_error" if anthropic else "upstream_error", "code": status}}
            prefix = "event: error\n" if anthropic else ""
            yield (prefix + f"data: {json.dumps(event, ensure_ascii=False)}\n\n").encode("utf-8")
    finally:
        # Close the nested Chat generator explicitly; do not wait for GC after
        # an adapter error, disconnect or cancellation.
        await _close_stream(upstream)


async def _stream_responses(url: str, headers: dict, body: dict,
                            model_name: str = "?", t0: float = 0.0, rid: str = "", cred=None):
    stream = _stream_adapted(url, headers, body, model_name, t0, rid, cred)
    try:
        async for chunk in stream:
            yield chunk
    finally:
        await _close_stream(stream)


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
        raise _bad_json(e) from None

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
    client_wants_stream = _client_wants_stream(payload)
    stream_policy = _snapshot_stream_policy("messages", chat_body)
    observe_stream_mode(stream_policy.mode)
    model_name = payload.get("model", "auto")
    chat_messages = chat_body.get("messages", [])
    rid = _request_id()
    _log(f"[{rid}] ▶ ANTHROPIC {model_name} | msgs={len(chat_messages)} | anthropic_msgs={len(messages)}")
    # Keep blocking credential selection and refresh off the event loop.
    prepared = chat_body        # Preserve canonical input for routing policy checks.
    chat_body, cred, headers, url = await run_in_threadpool(_route_chat, payload, chat_body, rid)
    _log_json(f"[{rid}] ANTHROPIC → CHAT BODY (预览)", chat_body)
    t0 = time.time()

    if not client_wants_stream:
        return await _nonstream_adapted(url, headers, chat_body, model_name, t0, rid, cred,
                                        anthropic=True, payload=payload, canonical=prepared,
                                        request=request, policy=stream_policy)

    def attempt(routed, cred, headers, url):
        return _stream_anthropic(url, headers, _body_with_stream_policy(routed, stream_policy),
                                 model_name, t0, rid, cred=cred)
    return _routed_stream(payload, prepared, model_name, rid, t0, attempt,
                          chat_body, cred, headers, url)


@app.post("/v1/search")
async def web_search_endpoint(request: Request,
                              authorization: Optional[str] = Header(default=None),
                              x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Serve generic REST web searches; returns the upstream result items unchanged."""
    _check_auth(authorization, x_api_key)
    try:
        payload = await request.json()
    except Exception as e:
        raise _bad_json(e) from None
    query = payload.get("query") if isinstance(payload, dict) else None
    if not isinstance(query, str) or not query.strip():
        raise HTTPException(status_code=400, detail={"error": {"message": "query 必须是非空字符串",
                                       "type": "invalid_request_error"}})
    max_results, problem = _validated_count(payload.get("max_results")
                                                 if isinstance(payload, dict) else None,
                                                 web_search.DEFAULT_RESULTS, 1,
                                                 web_search.MAX_RESULTS, "max_results")
    if problem:
        raise HTTPException(status_code=400, detail={"error": {"message": problem,
                                       "type": "invalid_request_error"}})
    results = await _web_search(query.strip(), max_results)
    return {"object": "search.results", "query": query, "total_results": len(results), "results": results}


@app.post("/v1/fetch")
async def web_fetch_endpoint(request: Request,
                             authorization: Optional[str] = Header(default=None),
                             x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Serve generic REST web fetches; returns the upstream content fields unchanged."""
    _check_auth(authorization, x_api_key)
    try:
        payload = await request.json()
    except Exception as e:
        raise _bad_json(e) from None
    try:
        url = web_fetch.validate_url(payload.get("url") if isinstance(payload, dict) else None)
    except ValueError as error:
        raise HTTPException(status_code=400, detail={"error": {"message": str(error),
                                         "type": "invalid_request_error"}}) from None
    prompt = payload.get("prompt") if isinstance(payload, dict) else None
    if prompt is not None and not isinstance(prompt, str):
        raise HTTPException(status_code=400, detail={"error": {"message": "prompt 必须是字符串",
                                         "type": "invalid_request_error"}})
    max_length, problem = _validated_count(payload.get("max_length")
                                               if isinstance(payload, dict) else None,
                                               web_fetch.DEFAULT_MAX_LENGTH, 1,
                                               web_fetch.MAX_LENGTH, "max_length")
    if problem:
        raise HTTPException(status_code=400, detail={"error": {"message": problem,
                                         "type": "invalid_request_error"}})
    result = await _web_fetch(url, prompt or "", max_length)
    return {"object": "fetch.result", **result}


def _bad_json(error: Exception) -> HTTPException:
    """400 for an unparsable JSON request body."""
    return HTTPException(status_code=400, detail={"error": {"message": f"bad json: {error}",
                                                            "type": "invalid_request_error"}})


def _validated_count(value, default: int, low: int, high: int, field: str) -> tuple[int, str]:
    """Accept an integer query field with bounds; errors stay user-visible (Chinese)."""
    if value is None:
        return default, ""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not float(value).is_integer():
        return 0, f"{field} 必须是整数"
    number = int(value)
    if number < low or number > high:
        return 0, f"{field} 必须在 {low} 到 {high} 之间"
    return number, ""


@app.post("/v1/images/generations")
async def images_generations(request: Request,
                             authorization: Optional[str] = Header(default=None),
                             x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Generate images through the upstream media endpoints."""
    _check_auth(authorization, x_api_key)
    payload = await _media_json(request)
    model = payload.get("model") or media_generation.DEFAULT_IMAGE_MODEL
    _media_check_model(model, "image")
    if not isinstance(payload.get("prompt"), str) or not payload["prompt"]:
        raise _media_bad_request("prompt 必须是非空字符串")
    n, problem = _validated_count(payload.get("n"), 1, 1, media_generation.MAX_IMAGE_ITEMS, "n")
    if problem:
        raise _media_bad_request(problem)
    if payload.get("response_format") not in (None, "url", "b64_json"):
        raise _media_bad_request("response_format 必须是 url 或 b64_json")
    size = _media_optional_text(payload.get("size"), "size", 64)
    quality = _media_optional_text(payload.get("quality"), "quality", 64)
    try:
        body = media_generation.image_request_body(model, payload["prompt"], size=size, n=n, quality=quality)
    except ValueError as error:
        raise _media_bad_request(str(error)) from None
    rid = _request_id()
    if bool(_media_base_url()) and payload.get("response_format") != "b64_json":
        _media_store()  # Refuse before the billed upstream round when persistence is impossible.
    _log(f"[{rid}] ▶ IMAGE | {model} | n={n} | chars={len(payload['prompt'])}")
    items, credit = await _media_call(MEDIA_IMAGE_GENERATE, body, rid, model=model,
                                      parse=media_generation.parse_image_reply)
    persist = bool(_media_base_url()) and payload.get("response_format") != "b64_json"
    data = await _media_image_reply(items, model, rid, persist=persist)
    response = {"created": int(time.time()), "data": data}
    if credit is not None:
        response["usage"] = {"credit": credit}
    return response


@app.post("/v1/images/edits")
async def images_edits(request: Request,
                       authorization: Optional[str] = Header(default=None),
                       x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Edit images through the upstream media endpoints; JSON body only, no multipart."""
    _check_auth(authorization, x_api_key)
    payload = await _media_json(request)
    model = payload.get("model") or media_generation.DEFAULT_IMAGE_EDIT_MODEL
    _media_check_model(model, "edit")
    if not isinstance(payload.get("prompt"), str) or not payload["prompt"]:
        raise _media_bad_request("prompt 必须是非空字符串")
    raw_images = payload.get("images")
    if not isinstance(raw_images, list) or not raw_images:
        raise _media_bad_request("images 必须是非空数组")
    images = []
    for entry in raw_images:
        url = entry.get("image_url") if isinstance(entry, dict) else None
        if not isinstance(url, str) or not url.strip():
            raise _media_bad_request("每个 images 项必须包含非空 image_url")
        images.append(url.strip())
    if len(images) > media_generation.MAX_IMAGE_ITEMS:
        raise _media_bad_request(f"images 最多 {media_generation.MAX_IMAGE_ITEMS} 项")
    if payload.get("response_format") not in (None, "url", "b64_json"):
        raise _media_bad_request("response_format 必须是 url 或 b64_json")
    size = _media_optional_text(payload.get("size"), "size", 64)
    quality = _media_optional_text(payload.get("quality"), "quality", 64)
    fidelity = _media_optional_text(payload.get("input_fidelity"), "input_fidelity", 64)
    try:
        body = media_generation.image_request_body(model, payload["prompt"], size=size, n=1,
                                                   quality=quality, images=images, input_fidelity=fidelity)
    except ValueError as error:
        raise _media_bad_request(str(error)) from None
    rid = _request_id()
    if bool(_media_base_url()) and payload.get("response_format") != "b64_json":
        _media_store()  # Refuse before the billed upstream round when persistence is impossible.
    _log(f"[{rid}] ▶ IMAGE EDIT | {model} | images={len(images)} | chars={len(payload['prompt'])}")
    items, credit = await _media_call(MEDIA_IMAGE_EDIT, body, rid, model=model,
                                      parse=media_generation.parse_image_reply)
    persist = bool(_media_base_url()) and payload.get("response_format") != "b64_json"
    data = await _media_image_reply(items, model, rid, persist=persist)
    response = {"created": int(time.time()), "data": data}
    if credit is not None:
        response["usage"] = {"credit": credit}
    return response


@app.post("/v1/videos")
async def create_video(request: Request,
                       authorization: Optional[str] = Header(default=None),
                       x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Submit one asynchronous video generation task."""
    _check_auth(authorization, x_api_key)
    payload = await _media_json(request)
    model = payload.get("model") or media_generation.DEFAULT_VIDEO_MODEL
    _media_check_model(model, "video")
    seconds = payload.get("seconds")
    if isinstance(seconds, str):
        try:
            seconds = int(seconds.strip())
        except ValueError:
            raise _media_bad_request("seconds 必须是整数或数字字符串") from None
    elif isinstance(seconds, float) and seconds.is_integer():
        seconds = int(seconds)
    try:
        body = media_generation.video_request_body(
            model,
            prompt=_media_optional_text(payload.get("prompt"), "prompt"),
            seconds=seconds,
            resolution=_media_optional_text(payload.get("resolution"), "resolution", 16),
            aspect_ratio=_media_optional_text(payload.get("aspect_ratio"), "aspect_ratio", 32),
            enable_audio=payload.get("enable_audio"),
            image_url=_media_optional_text(payload.get("image_url"), "image_url"))
    except ValueError as error:
        raise _media_bad_request(str(error)) from None
    store = _media_store()
    rid = _request_id()
    _log(f"[{rid}] ▶ VIDEO SUBMIT | {model} | chars={len(payload.get('prompt') or '')}")
    note = {}
    task_id = await _media_call(MEDIA_VIDEO_SUBMIT, body, rid, model=model,
                                parse=media_generation.parse_video_submit, lease_note=note)
    task = await run_in_threadpool(store.create_task, task_id, note.get("credential"),
                                   note.get("profile"), model)
    return {"id": task_id, "object": "video", "status": "queued", "model": model,
            "created_at": int((task or {}).get("created") or time.time())}


@app.get("/v1/videos/{task_id}")
async def get_video(task_id: str,
                    authorization: Optional[str] = Header(default=None),
                    x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Report one video task, refreshing in-flight state from its bound account."""
    _check_auth(authorization, x_api_key)
    task = await _media_video_refresh(task_id, _request_id())
    meta = None
    if task["status"] == "completed" and task["media_id"]:
        meta = await run_in_threadpool(_media_store().get_asset, task["media_id"])
    return _media_video_response(task, meta)


@app.get("/v1/videos/{task_id}/content")
async def get_video_content(task_id: str,
                            authorization: Optional[str] = Header(default=None),
                            x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Redirect to the public media URL of a finished video."""
    _check_auth(authorization, x_api_key)
    if not _media_base_url():
        raise HTTPException(status_code=409, detail={"error": {
            "message": "未配置资源访问地址（media_public_base_url）：视频已保存到网关本地，配置后即可获取链接",
            "type": "invalid_request_error", "code": "media_base_url_missing"}})
    task = await _media_video_refresh(task_id, _request_id())
    meta = None
    if task["status"] == "completed" and task["media_id"]:
        meta = await run_in_threadpool(_media_store().get_asset, task["media_id"])
    if meta is not None and media_store.is_expired(meta, _media_ttl_hours()):
        meta = None
    if meta is None:
        raise HTTPException(status_code=409, detail={"error": {
            "message": "视频资源尚不可用（任务未完成或资源已清理）",
            "type": "invalid_request_error", "code": "video_content_unavailable"}})
    return RedirectResponse(_media_public_url(meta), status_code=302)


@app.get("/v1/media/{kind}/{asset_id}", include_in_schema=False)
async def media_asset(kind: str, asset_id: str, request: Request):
    """Serve one stored media asset publicly with immutable caching headers."""
    if kind not in ("images", "videos") or not _MEDIA_ID.fullmatch(asset_id) \
            or asset_id.startswith("img_") != (kind == "images"):
        raise HTTPException(status_code=404, detail={"error": {
            "message": "媒体资源不存在", "type": "not_found_error"}})
    store = CONFIG.get("media_store")
    if store is None:
        raise HTTPException(status_code=503, detail={"error": {
            "message": "媒体存储不可用", "type": "service_unavailable"}})
    def lookup():
        meta = store.get_asset(asset_id)
        if meta is not None and media_store.is_expired(meta, _media_ttl_hours()):
            return None, None  # Expired reads fail now, not at the next hourly sweep.
        return (meta, store.asset_file(meta)) if meta is not None else (None, None)

    meta, file = await run_in_threadpool(lookup)
    if file is None:
        raise HTTPException(status_code=404, detail={"error": {
            "message": "媒体资源不存在或已清理", "type": "not_found_error"}})
    if request.headers.get("if-none-match") == f'"{meta["sha256"]}"':
        return Response(status_code=304, headers=media_store.security_headers(meta))
    return FileResponse(file, media_type=meta["mime"], headers=media_store.security_headers(meta))


@app.post("/v1/mcp")
async def mcp_endpoint(request: Request,
                       authorization: Optional[str] = Header(default=None),
                       x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Serve the stateless MCP streamable-HTTP surface exposing the web_search and web_fetch tools."""
    _check_auth(authorization, x_api_key)
    origin = request.headers.get("origin")
    if origin:
        # 只有浏览器会带 Origin（CSRF/DNS rebinding 防线）；MCP 客户端从不带。
        return JSONResponse({"error": {"message": "browser origins are not accepted on /v1/mcp",
                                       "type": "invalid_request_error"}}, status_code=403)
    try:
        message = await request.json()
    except ValueError:
        return JSONResponse(mcp_server.error_body(None, mcp_server.JSONRPC_PARSE_ERROR, "Parse error"),
                            status_code=400)
    executes = (isinstance(message, dict) and message.get("method") == "tools/call"
                and message.get("id") is not None)
    if not executes:
        # 协议握手与列表请求不产生上游调用，不进入请求统计。
        observe_discard()
    status, body = await mcp_server.dispatch(
        request.headers, message,
        {"web_search": _mcp_tool(_web_search, "web search failed"),
         "web_fetch": _mcp_tool(_web_fetch, "web fetch failed")})
    if status == 202 or body is None:
        return Response(status_code=202)
    return JSONResponse(body, status_code=status)


async def _stream_anthropic(url: str, headers: dict, body: dict,
                            model_name: str = "?", t0: float = 0.0, rid: str = "", cred=None):
    stream = _stream_adapted(url, headers, body, model_name, t0, rid, cred, anthropic=True)
    try:
        async for chunk in stream:
            yield chunk
    finally:
        await _close_stream(stream)


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request,
                       authorization: Optional[str] = Header(default=None),
                       x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Return heuristic token estimates for Anthropic request budgeting."""
    _check_auth(authorization, x_api_key)
    try:
        payload = await request.json()
    except Exception as e:
        raise _bad_json(e) from None
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

def _projection_bytes_arg(value):
    number = _nonnegative_int(value)
    if number != 0 and number < 256:
        raise argparse.ArgumentTypeError("必须为 0 或至少 256")
    if number > 33554432:
        raise argparse.ArgumentTypeError("不能超过 33554432")
    return number


def _origins_arg(value):
    from app.settings import normalize_allowed_origins
    try:
        return normalize_allowed_origins(value)
    except ValueError:
        raise argparse.ArgumentTypeError("必须为逗号分隔的 http/https 来源或域名") from None


def _boolean_arg(value):
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in ("true", "1", "yes", "on"):
        return True
    if normalized in ("false", "0", "no", "off"):
        return False
    raise argparse.ArgumentTypeError("必须为 true 或 false")


def _is_config_invocation(argv) -> bool:
    """True when the first non-option token is the offline config command."""
    for token in argv:
        if token.startswith("-"):
            continue
        return token == "config"
    return False


def _persist_cli_language(language, store=None, config=None) -> None:
    """`--lang` writes the interface language once; an unchanged value never bumps the revision."""
    if not language:
        return
    owned = store is None
    if store is None:
        from app.control_store import ControlStore
        path = managed_auth_dir() / "control.sqlite3"
        if not path.exists():
            sys.stderr.write("[lang] 配置库尚未创建，语言仅本次运行生效\n")
            return
        store = ControlStore(path)
    try:
        snapshot = store.snapshot()
        if snapshot["settings"].get("language") != language:
            store.update_settings({"language": language}, snapshot["revision"])
        if config is not None:
            config["language"] = language
            config.setdefault("settings_sources", {})["language"] = "cli"
    except Exception as error:
        sys.stderr.write(f"[lang] 语言未保存: {error}\n")
    finally:
        if owned:
            store.close()


def _config_keys(raw):
    keys = [item.strip() for item in str(raw or "").split(",") if item.strip()]
    return keys or None


def _config_status_lines(report, lang) -> list:
    from app import i18n as cli_i18n

    env_path = report["env_path"] or cli_i18n.tr(lang, "config.noEnv")
    lease = report["lease"]
    if lease and report["lease_fresh"]:
        lease_text = cli_i18n.tr(lang, "config.running", pid=lease["pid"],
                                 age=int(time.time() - lease["heartbeat_at"]))
    elif lease:
        lease_text = cli_i18n.tr(lang, "config.released")
    else:
        lease_text = cli_i18n.tr(lang, "config.idle")
    pending = cli_i18n.tr(lang, "config.pending", count=len(report["changes"]))
    if report["changes"]:
        pending += f"（{', '.join(report['changes'])}）" if lang == "zh" else f" ({', '.join(report['changes'])})"
    return [
        cli_i18n.tr(lang, "config.envFile", path=env_path),
        cli_i18n.tr(lang, "config.target", path=report["db_path"]),
        cli_i18n.tr(lang, "config.schema", version=report["version"],
                    revision=report["revision"], accounts=report["accounts"]),
        cli_i18n.tr(lang, "config.state", state=lease_text),
        pending,
        cli_i18n.tr(lang, "config.unchanged", count=len(report["unchanged"])),
    ]


def config_command(args) -> int:
    """Offline `config migrate|status`; never starts the server or touches credentials."""
    from app import config_migration
    from app import i18n as cli_i18n

    if args.action is None:
        sys.stderr.write(cli_i18n.tr(cli_i18n.resolve_language(args.lang), "config.usage") + "\n")
        return 2
    keys = _config_keys(args.keys)
    options = {"env_file": args.env_file, "no_env": args.no_env,
               "data_dir": args.data_dir, "db": args.db}
    try:
        if args.action == "status":
            targets = config_migration.resolve_targets(**options)
            info = config_migration.inspect_database(targets["db_path"])
            values = config_migration.collect_runtime_values(targets["env_values"])
            planned = config_migration.plan(values, info["settings"], keys=keys)
            report = {"env_path": targets["env_path"], "db_path": targets["db_path"],
                      "version": info["version"], "revision": info["revision"],
                      "accounts": info["accounts"], "lease": info["lease"],
                      "lease_fresh": config_migration.lease_is_fresh(info["lease"]),
                      **planned}
            lang = cli_i18n.resolve_language(args.lang or info["settings"].get("language"))
            for line in _config_status_lines(report, lang):
                print(line)
            return 0
        report = config_migration.migrate(**options, dry_run=args.dry_run, force=args.force,
                                          keys=keys, language=args.lang)
    except config_migration.MigrationError as error:
        sys.stderr.write(f"[config] {error}\n")
        return 2
    lang = cli_i18n.resolve_language(args.lang or report.get("language")
                                     or config_migration.inspect_database(report["db_path"])["settings"].get("language"))
    for line in _config_status_lines(report, lang):
        print(line)
    if report["dry_run"]:
        print(cli_i18n.tr(lang, "config.dryRun"))
        return 0
    if not report["written"]:
        print(cli_i18n.tr(lang, "config.noChanges"))
        return 0
    env_names = []
    for key, value in report["changes"].items():
        name = SCHEMA[key].get("env")
        if name:
            rendered = str(value).lower() if isinstance(value, bool) else value
            env_names.append(f"{name}={rendered}")
    print(cli_i18n.tr(lang, "config.written", count=len(report["changes"]),
                      revision=report["revision"]))
    if report.get("language"):
        print(cli_i18n.tr(lang, "config.language", language=report["language"]))
    if env_names:
        print(cli_i18n.tr(lang, "config.removeEnv"))
        for line in env_names:
            print(f"  {line}")
    return 0


def main():
    launch = capture_launch()  # Before .env loading, so a restart re-reads .env as a fresh start would.
    offline_config = _is_config_invocation(sys.argv[1:])
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]) or offline_config:
        dotenv_keys = set()
    else:
        dotenv_keys = load_startup_env()
    ap = argparse.ArgumentParser(description="CodeBuddy -> OpenAI 兼容转换器（直连后端）")
    ap.add_argument("command", nargs="?", choices=("serve", "login", "config"), default="serve",
                    help="serve 启动服务（默认）；login 扫码登录；config 离线配置迁移（migrate/status）")
    ap.add_argument("action", nargs="?", choices=("migrate", "status"),
                    help="config 子命令：migrate 写入配置库；status 只读查看解析结果")
    ap.add_argument("--env-file", metavar="PATH",
                    help="config 读取的 env 文件；默认当前目录 .env")
    ap.add_argument("--no-env", action="store_true",
                    help="config 不读取 env 文件，只使用进程环境变量")
    ap.add_argument("--data-dir", metavar="PATH",
                    help="config 目标数据目录（其中含 control.sqlite3）；默认按 CODEBUDDY_AUTH_DIR 解析")
    ap.add_argument("--db", metavar="PATH",
                    help="config 直接指定 control.sqlite3 路径")
    ap.add_argument("--dry-run", action="store_true", help="config migrate 只打印差异，不写库")
    ap.add_argument("--keys", metavar="LIST", help="config 仅处理逗号分隔的配置项")
    ap.add_argument("--force", action="store_true",
                    help="config migrate 在检测到运行中实例时仍然写入（危险）")
    ap.add_argument("--lang", choices=("auto", "zh", "en"),
                    help="界面与本次 CLI 输出语言；带该参数启动会把语言写入配置库（config status 只读）")
    ap.add_argument("--site", choices=tuple(auth_oauth.SITE_HOSTS), default="cn",
                    help="login 站点：cn 国内站（默认）；intl 国际 WorkBuddy；intl-codebuddy 国际 CodeBuddy")
    ap.add_argument("--no-browser", action="store_true",
                    help="login 仅显示授权链接，不自动打开浏览器（服务器/容器环境）")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址；覆盖 CODEBUDDY2API_BIND")
    ap.add_argument("--port", type=int, default=8787, help="监听端口；覆盖 CODEBUDDY2API_PORT")
    ap.add_argument("--api-key", default=os.environ.get("CODEBUDDY2API_KEY", ""),
                    help="管理与推理密钥；未配置时首次本地交互启动生成并保存")
    ap.add_argument("--admin-csrf", type=_boolean_arg, nargs="?", const=True,
                    default=os.environ.get("CODEBUDDY2API_ADMIN_CSRF", "true"),
                    help="管理 Origin/CSRF 校验，默认 true；仅在受信任本地环境设为 false，鉴权仍启用")
    ap.add_argument("--admin-allowed-origins", type=_origins_arg, metavar="ORIGINS",
                    default=os.environ.get("CODEBUDDY2API_ADMIN_ORIGINS"),
                    help="额外信任的管理页来源（逗号分隔，支持域名或完整来源，裸域名按 https）；"
                         "反代 HTTPS 域名登录报 Origin 校验失败时设置，也可在 WebUI 配置")
    ap.add_argument("--log", default=None, metavar="PATH",
                    help="已停用：日志统一保存到数据目录中的 logs.sqlite3")
    ap.add_argument("--desensitize", action="store_true",
                    help="适配固定 CLI 模板、压缩运行时提示并零宽脱敏关键词。默认关闭。")
    ap.add_argument("--no-compact", action="store_true",
                    help="配合 --desensitize 保留主要行为指令，仍适配固定模板并裁剪运行时元数据；"
                         "非流式纯审核拒绝最多压缩兜底一次。")
    ap.add_argument("--keep-tool-metadata", type=_boolean_arg, nargs="?", const=True,
                    default=os.environ.get("CODEBUDDY2API_KEEP_TOOL_METADATA", "false"),
                    help="保留工具描述及参数 description/title；启用脱敏时仍处理描述文本，默认 false")
    ap.add_argument("--responses-projection-mode", choices=PROJECTION_MODES,
                    default=os.environ.get("CODEBUDDY2API_RESPONSES_PROJECTION_MODE", "balanced"),
                    help="Responses 上下文：balanced 仅改写有固定摘要的已识别 harness，其余文本原样保留；passthrough 完全关闭投影；默认 balanced")
    ap.add_argument("--responses-projection-max-bytes", type=_projection_bytes_arg, metavar="BYTES",
                    default=os.environ.get("CODEBUDDY2API_RESPONSES_PROJECTION_MAX_BYTES", "40000"),
                    help="Responses 单项 UTF-8 字节上限；0 禁用，非零范围 256..33554432，默认 40000")
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
    ap.add_argument("--stream-mode", choices=_STREAM_MODES,
                    default=os.environ.get("CODEBUDDY2API_STREAM_MODE", "compatible"),
                    help="流式传输：compatible 保持兼容聚合策略，realtime 增量发送且不重生成工具参数；默认 compatible")
    ap.add_argument("--catalog-probe-mode", choices=("account", "representative"),
                    default=os.environ.get("CODEBUDDY2API_CATALOG_PROBE_MODE", "account"),
                    help="目录探测：account 逐个账号刷新（默认）；representative 每个产品每周期只探测一个代表账号并补齐缺目录账号")
    ap.add_argument("--routing-scheduler", choices=("legacy", "shadow", "indexed"),
                    default=os.environ.get("CODEBUDDY2API_ROUTING_SCHEDULER", "shadow"),
                    help="路由调度：shadow 记录新旧差异并仍返回旧结果（默认，差异为零后可切 indexed）；indexed 复用按模型分片；legacy 每次全量判断")
    ap.add_argument("--model-capability-guard", type=_boolean_arg, nargs="?", const=True,
                    default=os.environ.get("CODEBUDDY2API_MODEL_CAPABILITY_GUARD", "true"),
                    help="按账号模型声明预检图片、工具、思考和输出上限；false 仅关闭新增能力预检")
    ap.add_argument("--log-body-limit", type=_nonnegative_int, metavar="BYTES",
                    default=os.environ.get("CODEBUDDY2API_LOG_BODY_LIMIT", "65536"),
                    help="旧文本预览兼容项；文本输出已停用，SQLite 诊断使用独立预算")
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
    if args.command == "config":
        return config_command(args)
    if args.command == "login":
        _persist_cli_language(args.lang)
        return login(site=args.site, open_browser=not args.no_browser)

    for key in ("max_images", "image_policy", "max_request_bytes", "log_body_limit",
                "tool_call_max_retry", "max_inbound_bytes", "max_collect_bytes", "max_concurrent",
                "failover_max", "retry_write_timeout", "upstream_keepalive", "max_inflight_per_account",
                "request_context_mode", "stream_mode", "model_capability_guard", "admin_allowed_origins",
                "responses_projection_mode", "responses_projection_max_bytes", "routing_scheduler",
                "catalog_probe_mode"):
        CONFIG[key] = getattr(args, key)
    CONFIG["api_key"] = args.api_key
    CONFIG["desensitize"] = args.desensitize
    CONFIG["no_compact"] = args.no_compact
    CONFIG["credit_price_cny"] = args.credit_price_cny or None
    CONFIG["usd_rate"] = args.usd_rate or None
    CONFIG["credit_price_usd"] = args.credit_price_usd or None
    CONFIG["model_guard"] = not args.no_model_guard
    if args.log is not None or os.environ.get("CODEBUDDY2API_LOG"):
        sys.stderr.write("[log] --log / CODEBUDDY2API_LOG 已停用；请在 WebUI 查看 SQLite 日志。\n")
    CONFIG["log_path"] = None
    from app import runtime_management
    runtime_management.initialize(sys.modules[__name__], args, parser=ap, dotenv_keys=dotenv_keys)
    # The running-instance lease is claimed with the other background threads, after
    # catalog and usage hydration, so an offline tool never races the first write.
    control_store = CONFIG.get("control_store")
    # Validate effective binding and authentication before credential scans or background work.
    if (args.host not in ("127.0.0.1", "::1", "localhost") and not CONFIG.get("api_key")
            and not allows_open_noauth()):
        runtime_management.close(CONFIG)
        ap.error("非回环绑定且未设置 API key 会匿名开放推理额度；"
                 "请设置 CODEBUDDY2API_KEY，或确知风险后以 CODEBUDDY2API_ALLOW_OPEN_NOAUTH=true 显式放行")
    files = [Path(p) for p in args.auth_file]
    if not files:
        seed_credentials()  # Seed missing desktop credentials into managed storage.
    CONFIG["cred_pool"] = CredentialPool(files, scan=not files, state_store=CONFIG["state_store"])
    CONFIG["usage_snapshots"] = UsageSnapshots(store=CONFIG["state_store"])
    CONFIG["cred"] = CONFIG["cred_pool"].first()
    CONFIG["account_catalogs"] = {}  # Disable static fallback before maintenance starts.
    CONFIG["rate_book"] = RateBook(manual=lambda: model_rates.manual_rates(CONFIG))
    if credits_mod is not None:
        ledger = credits_mod.CreditLedger(store=CONFIG["state_store"])
        CONFIG["ledger"] = ledger
        CONFIG["model_cache"] = credits_mod.ModelCatalogCache(ttl=args.model_catalog_ttl, store=CONFIG["state_store"])
        CONFIG["aggregate_catalog"] = AggregateCatalog(ttl=args.model_catalog_ttl, store=CONFIG["state_store"])
        CONFIG["cred_pool"].set_ledger(ledger)  # Verify balance ownership before publishing catalogs.
    _publish_model_cache()
    # Publish cached usage before maintenance threads start, so the dashboard is populated from
    # the first request. It stays a no-op when nothing is cached, so an empty deployment and a
    # mocked pool are both unaffected.
    if CONFIG["usage_snapshots"].detail():
        _publish_usage_daily(CONFIG["cred_pool"])

    try:
        runtime_management.install(sys.modules[__name__])
    except SessionStoreError as error:
        # An obsolete session snapshot survived, so the new key epoch must not activate:
        # a later start under the superseded key could adopt it and revive admin cookies.
        runtime_management.close(CONFIG)
        ap.error(str(error))
    model_rates.refresh(sys.modules[__name__])  # Audit and control stores are open from here on.
    if control_store is not None:
        control_store.claim_service_lease()
        _persist_cli_language(args.lang, control_store, CONFIG)
        threading.Thread(target=_lease_loop, args=(control_store,),
                         daemon=True, name="service-lease").start()
    threading.Thread(target=_refresher_loop, args=(CONFIG["cred_pool"],),
                     daemon=True, name="cred-refresher").start()
    threading.Thread(target=_reconcile_loop, args=(CONFIG["cred_pool"],),
                     daemon=True, name="cred-reconcile").start()
    if credits_mod is not None:
        threading.Thread(target=_housekeeper_loop, args=(CONFIG["cred_pool"], ledger),
                         daemon=True, name="cred-housekeeper").start()
    if (updater := CONFIG.get("updater")) is not None:
        threading.Thread(target=updater.check_loop, daemon=True, name="update-check").start()
    if (backups := CONFIG.get("backups")) is not None:
        threading.Thread(target=backups.loop, daemon=True, name="config-backup").start()

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
        sys.stderr.write("   POST /admin/credentials/{id}/reset-cooldown  (清除该账号冷却，仅本地)\n")
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

    CONFIG["launch"] = launch
    try:
        run_server(app, CONFIG, host=args.host, port=args.port)
    finally:
        if CONFIG.get("control_store") is not None:
            try:
                CONFIG["control_store"].release_service_lease()
            except Exception:
                pass
        runtime_management.close(CONFIG)
    if CONFIG.get("restart_requested"):
        # A self-update swapped the program files; start them with the original invocation.
        sys.stderr.write("[update] 正在按原启动参数重启…\n")
        restart_process(launch)


if __name__ == "__main__":
    sys.exit(main())
