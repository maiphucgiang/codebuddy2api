#!/usr/bin/env python3
"""
codebuddy2api — 把 CodeBuddy / WorkBuddy 的订阅暴露成标准 OpenAI 兼容 API。

原理（直连后端，原生 function calling）：
  - 读取本机已登录的 CodeBuddy 桌面端凭据（auth 文件里的 token / uid / enterpriseId）。
  - 直接转发到 CodeBuddy 后端 `https://copilot.tencent.com/v2/chat/completions`。
    该后端本身就是标准 OpenAI chat/completions 协议（含原生 tools / tool_calls / SSE 流式）。
  - 转换器只做两件事：①注入鉴权 header（Authorization / X-User-Id 等）
    ②在本地 /v1/* 与后端 /v2/* 之间做路径映射与透传（含 Anthropic / Chat / Responses 三种协议）。
  - token 过期时自动调 `/v2/plugin/auth/token/refresh` 刷新，并回写 auth 文件。
  - 支持无感登录采集新凭证（/admin/oauth/start + /admin/oauth/poll，OAuth state 轮询），
    种子/导入/无感登录入库统一做站点白名单校验；距上次刷新超 24h 每日保活刷新。
跨平台：自动定位 auth 目录（macOS / Windows / Linux）。
依赖：fastapi + uvicorn + httpx（pip install fastapi "uvicorn[standard]" httpx）。

用法：
  python3 converter.py                       # 默认 127.0.0.1:8787
  python3 converter.py --port 9000
  python3 converter.py --api-key mysecret    # 启用客户端鉴权
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
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
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
import uvicorn

try:
    from app.desensitize import desensitize_body
except ImportError:  # 模块缺失时降级为不脱敏
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
from app import checkin as checkin_service, model_policy, travel
from app.model_blocks import ModelBlocks
from app.observability import (AuditMiddleware, observe_recovery, observe_route,
                               observe_usage, observe_attempt, observe_failure,
                               observe_failure_seq)
from app.credential_io import (CredentialFileError, read_import_file, atomic_write_credential,
                               credential_file_lock)
from app.upstream_io import (ChatSSEAccumulator, UpstreamHTTPError, UpstreamResponseError,
                             open_backend_stream, read_bounded_error)
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
except ImportError:  # 模块缺失时签到/积分/快过期优先调度不可用
    credits_mod = None

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

APP_VERSION = Path(__file__).with_name("VERSION").read_text(encoding="utf-8").strip()
BACKEND = "https://copilot.tencent.com"
DEFAULT_DOMAIN = "www.codebuddy.cn"
CBC_VERSION = CLI_VERSION
USER_AGENT = CLI_USER_AGENT

# ---------------------------------------------------------------------------
# 平台相关：定位 auth 目录
# ---------------------------------------------------------------------------

def managed_auth_dir() -> Path:
    """自管凭证目录：CODEBUDDY_AUTH_DIR 已设置则用其（容器挂载场景），否则项目下 auth/。"""
    env_dir = os.environ.get("CODEBUDDY_AUTH_DIR")
    return Path(env_dir) if env_dir else Path(__file__).resolve().parent / "auth"


def auth_dirs() -> list[Path]:
    """桌面端登录态目录（仅作种子来源，不直接挂进池）。"""
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
    """把桌面端已登录凭据复制进自管目录（只补缺失文件，不覆盖）。CODEBUDDY_AUTH_DIR 模式跳过。"""
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
    """扫描自管目录下的全部 *.info 凭据文件。"""
    d = managed_auth_dir()
    return sorted(d.glob("*.info")) if d.is_dir() else []


def _cred_uid(path) -> Optional[str]:
    """读取凭据文件的 account.uid（兼容 accounts[0]）作为账号去重键；读不出返回 None。"""
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
# Auth 凭据管理（读 + 自动刷新 + 回写）
# ---------------------------------------------------------------------------

class CredentialManager:
    """从 auth 文件读取凭据；token 临近过期时自动刷新并回写。"""

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
        """原子替换或外部更新后重读，并使旧请求持有的凭据代次失效。"""
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
        # 提前 60s 判定过期
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
        """与导入共享文件锁，避免刷新旧会话覆盖刚保存的新登录态。"""
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
        """返回带最新 token 的后端请求 header；必要时先刷新。"""
        with self._lock:
            if self._is_expired():
                self._refresh()
            s = self._session()
            return self._build_headers_from(s.get("auth") or {}, _credential_account(s))

    def refresh_if_due(self, margin_s: int, keepalive_s: int) -> bool:
        """后台和前台使用同一个刷新临界区与条件复查。"""
        return self._refresh(margin_s, keepalive_s)

    def invalidate(self):
        """显式导入后重新读取磁盘，但保留管理器与刷新锁。"""
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


STICKY_TTL = 30 * 60        # 会话黏绑闲置解绑秒数
STICKY_MAX = 512            # 黏绑表容量上限
CRED_COOLDOWN = 300         # 凭证熔断冷却秒数
MODEL_COOLDOWN = 600        # 模型级冷却兜底秒数（429 错误体无重置时间时）
MODEL_COOLDOWN_MAX = 86400  # 模型级冷却上限秒数
MODEL_SITE_BLOCK_S = 6 * 3600      # 官方判定「该后端无此模型」后的首次避让时长
MODEL_SITE_BLOCK_MAX_S = 24 * 3600  # 反复命中的退避上限：最多一天放行重试一次
# 后端确定性答复：这个站点根本没有这个模型（重试无意义，只能换后端）。
MODEL_NOT_SERVABLE_CODES = frozenset({"11102"})
_NOT_SERVABLE_MSG = re.compile(r"service info not found|model .{0,80}not (?:found|supported)", re.I)
CRED_REFRESH_MARGIN = 600   # 主动刷新提前量秒数
CRED_KEEPALIVE_S = 24 * 3600   # 每日保活：距上次刷新超过该值即主动刷新，防 refresh token 闲置过期
CRED_KEEPALIVE_RETRY_S = 3600  # 保活刷新失败后的重试间隔（与临期刷新失败解耦）


def _msg_text(m: dict) -> str:
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(p.get("text", "") for p in c if isinstance(p, dict))
    return ""


def session_key(payload: dict) -> str | None:
    """会话身份：system + 首条 user 消息哈希。同一会话各轮稳定，跨会话不同。"""
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
    """从 429 错误体解析配额重置时间（如 '将在 2026-08-29 22:32:31 UTC+8 重置'），返回 epoch 秒。"""
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
    """识别 11102 之类的「该后端无此模型」答复，返回 (code, msg)；不是则 None。

    只比对 code/msg 等独立字段：错误体里还带着 requestId，拿整段文本做子串匹配会把
    "11102" 撞在 ID 上，误避让一个本来能用的模型。
    """
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
    """避让表按客户端可见的模型名记账：default-model 只是 intl 侧对 auto 的别名。"""
    return "auto" if model == "default-model" else model


def _dynamic_request_headers(skey: str | None) -> dict:
    """每次请求生成与官方客户端同构的追踪/请求 ID 头；会话 ID 随 session_key 稳定。"""
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
    """多凭证池：目录发现 + 热加载、黏性会话绑定、健康熔断、主动刷新。"""

    def __init__(self, paths: list[Path] | None = None, scan: bool = False,
                 blocks_path: Path | None = None):
        self._lock = threading.RLock()
        self._entries: list[dict] = []   # {id, cm, fail_until}
        self._sticky: "OrderedDict[str, tuple[str, float]]" = OrderedDict()
        self._model_fail: dict[tuple[str, str], float] = {}  # (cred_id, model) -> 冷却截止 epoch（429 模型级冷却）
        # (后端, 模型) -> 避让截止：官方回 11102 说明该后端根本没这个模型，路由自动绕开
        self._blocks = ModelBlocks(blocks_path, ttl_s=MODEL_SITE_BLOCK_S, max_ttl_s=MODEL_SITE_BLOCK_MAX_S)
        self._rr = {None: 0, "cn": 0, "intl": 0}
        self._ledger = None              # CreditLedger：pick 时按积分最早过期时间优先调度
        self._scan = scan                # True 时 pick 前自动扫描目录增删凭证
        self._ignored_duplicates: set[str] = set()
        self._sync_pending: set[str] = set()
        self._syncing: set[str] = set()
        self._sync_event = threading.Event()
        self._sync_retry: dict[str, float] = {}
        self._sync_attempts: dict[str, int] = {}
        self.reload(paths or [])
        if self._scan:
            self._rescan()             # 启动即发现一轮，/health 不等首个请求

    def reload(self, paths: list[Path], *, reset: bool = True):
        """只在文件实际更新或显式导入时重置认证状态，并通知目录刷新。"""
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
                        continue  # 单个损坏文件不能阻止其他凭据被发现
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
        """消费待刷队列；事件和队列在同一把锁下清除，避免丢失唤醒。"""
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
        """过期请求的额度或目录结果不能覆盖新登录态的缓存。"""
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
        """移除已不存在文件的凭据，并清理其黏绑。"""
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
        """按账号 uid 查池内凭据 id（用于导入冲突检测）。"""
        with self._lock:
            for e in self._entries:
                if e.get("uid") == uid and (identity is None or e.get("account_key") == identity):
                    return e["id"]
        return None

    def set_ledger(self, ledger):
        """挂接 CreditLedger 后，pick 按积分最早过期时间优先选凭证。"""
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
        """池内凭证条目快照（供签到/积分调度遍历）。"""
        with self._lock:
            return [dict(e) for e in self._entries]

    def _expiry_rank(self, e: dict) -> tuple:
        """快过期优先排序键：(无数据排后, 最早过期时间升序)。"""
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
        """该账号已确认余额为 0：只能使用目录声明的零倍率模型。"""
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
            # 关闭 guard 仅允许单产品的明确表外透传，不能把 A 的已知能力借给 B。
            declared = any(item["id"] == _upstream_model(model, profile)
                           for item in _models_for_profile(profile, configured, scope="serves"))
            passthrough = (model != "auto" and not declared and not CONFIG.get("model_guard")
                           and len(configured) == 1)
            if model and not (supported or cli_auto or passthrough):
                return False
        # 零余额账号退出付费模型轮询，只保留自身目录声明为 x0.00 的模型。
        return (not model or self._has_credit(entry, profile)
                or self._model_free(entry, model, profile=profile))

    def _model_free(self, entry, model: str | None, *, profile=None) -> bool:
        """该凭证的账号目录是否把此模型声明为零计费（x0.00）。"""
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
        """该凭证实际打的后端入口：模型可用性按入口判定，同站点不同产品互不牵连。"""
        profile = cls._entry_profile(e)
        return PROFILE_ENDPOINTS.get(profile) if profile else None

    def _model_servable(self, e: dict, model: str | None) -> bool:
        """该后端未处于「无此模型」避让期；model 为空时不做后端级检查。"""
        if not model:
            return True
        endpoint = self._entry_endpoint(e)
        if not endpoint:
            return True
        return time.time() >= self._blocks.until(endpoint, _block_model(model))

    def _model_healthy(self, e: dict, model: str | None) -> bool:
        """该凭证对指定模型未处于 429 冷却期；model 为空时不做模型级检查。"""
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
        """可用凭证按（零计费优先, 快过期积分优先）排序；同级由调用方轮询。

        `tried` 是本轮已经打过的凭证管理器：换凭证重放时把它们排除在候选外，避免又选回
        同一个刚失败的站点。
        """
        tried = set(tried)
        healthy = [entry for entry in self._entries if entry["cm"] not in tried
                   and self._healthy(entry)
                   and self._eligible(entry, model, region=region) and self._model_healthy(entry, model)
                   and self._model_servable(entry, model)]
        if not healthy:
            return []
        # 目录倍率 x0.00 的同名模型排最前，其次快过期积分优先；无数据排最后。
        healthy.sort(key=lambda entry: (not self._model_free(entry, model), *self._expiry_rank(entry)))
        return healthy

    def pick(self, skey: str | None, model: str | None = None, *, region=None,
             tried=()) -> CredentialManager | None:
        """按黏绑选凭证；未绑定/已失效则轮询取健康凭证并绑定。

        model 非空时跳过该模型 429 冷却中的凭证（黏性会话自动换绑）；
        全部凭证对该模型冷却时返回 None，由上层快速失败，不再打上游。
        候选优先零计费账号；黏绑账号被更好的来源替代时自动重绑。
        """
        self._rescan()  # 锁外扫描，reload/prune 各自取锁，避免死锁
        with self._lock:
            self._evict_sticky()
            candidates = self._candidates(model, region=region, tried=tried)
            if not candidates:
                if skey:
                    self._sticky.pop(skey, None)
                return None
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
                    with_generation=False, tried=()):
        """在发送前复核凭据代次和站点，避免重载竞态导致跨站调用。"""
        for _ in range(max(1, len(self._entries))):
            cm = self.pick(skey, model, region=region, tried=tried)
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
                    return ((cm, generation) if with_generation else cm), headers
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
                    model: str | None = None, raw: bytes = b"", *, generation=None):
        """401/403 熔断整个凭证；429 只冷却 (凭证,模型) 至配额重置时间；11102 按 (后端,模型) 避让。

        三者都是局部降级：其他模型、其他凭证、其他后端不受影响。"""
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
        until = _parse_reset_time(raw) or now + MODEL_COOLDOWN
        until = min(until, now + MODEL_COOLDOWN_MAX)
        with self._lock, (cm._lock if generation is not None else nullcontext()):
            if not self._lease_matches(cm, generation):
                return
            self._model_fail = {k: v for k, v in self._model_fail.items() if v > now}
            for e in self._entries:
                if e["cm"] is cm:
                    routed_model = _upstream_model(model, self._entry_profile(e))
                    self._model_fail[(e["id"], routed_model)] = until
        _log(f"[cred] 模型冷却 {model} @ {Path(cm.path).name} 至 "
             f"{time.strftime('%m-%d %H:%M:%S', time.localtime(until))} (HTTP 429)")

    def model_cooldown_until(self, model: str | None, *, region=None) -> float | None:
        """该模型在所有健康凭证上都在冷却时返回最早恢复时间；否则 None。"""
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
        """记下「这个后端没有这个模型」，返回解除时间；路由会自动绕开该后端。"""
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
        """该后端实测认这个模型了：立刻解除避让，不必等 TTL 半开。"""
        if not model:
            return False
        entry = next((e for e in self._entries if e["cm"] is cm), None)
        endpoint = self._entry_endpoint(entry) if entry else None
        return bool(endpoint) and self._blocks.clear(endpoint, _block_model(model))

    def model_block_until(self, model: str | None, *, region=None) -> float | None:
        """所有潜在后端均有实测避让时返回解除时间；未知目录不等于不支持。"""
        if not model:
            return None
        now = time.time()
        with self._lock:
            candidates = [e for e in self._entries
                          if self._healthy(e) and (region is None
                                                   or _in_region(self._entry_profile(e), region))]
            endpoints = {self._entry_endpoint(e) for e in candidates}
            # 已知不支持的后端不抵消避让；未知目录仍是潜在来源，但不获得派发资格。
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
        """避让表明细（看板/排障用）。"""
        return self._blocks.detail()

    def refresh_due(self, margin_s: int = CRED_REFRESH_MARGIN, keepalive_s: int = CRED_KEEPALIVE_S):
        """按到期与保活条件刷新，失败退避只作用于发起操作时的凭据代次。"""
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
        """删除与刷新共用锁，避免删除后被在途刷新重新创建。"""
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
    """后台主动刷新：过期前刷新并回写，凭证不因闲置而失效。"""
    while True:
        time.sleep(60)
        try:
            pool.refresh_due()
        except Exception as e:
            _log(f"[cred] 刷新线程异常: {e}")

CHECKIN_FIRST_DELAY = 30     # 启动后首次签到延迟秒数
HOUSEKEEP_INTERVAL = 3600    # 签到兜底 + 积分刷新周期秒数


def _bearer_token(headers: dict) -> str:
    return (headers.get("Authorization") or "").removeprefix("Bearer ").strip()


_HOUSEKEEP_LOCK = threading.Lock()


def _sync_error(pool, ledger, entry, generation, phase, error):
    message = f"{phase}: {_network_error_text(error)}"
    pool.apply_if_current(entry["cm"], generation, lambda: ledger.note_error(entry["id"], message))
    _log(f"[{phase}] {Path(entry['id']).name} 同步失败（保留旧数据）: {message}")


def _sync_trial(headers):
    """可选福利领取与余额同步分离，持久化故障不阻断普通请求。"""
    ledger = CONFIG.get("trial_ledger")
    if not CONFIG.get("auto_trial") or ledger is None:
        return
    profile, uid = profile_for_headers(headers), headers.get("X-User-Id", "")
    if profile != "intl-work" or not uid:
        return
    key = account_key(profile, uid, headers.get("X-Enterprise-Id", ""))
    try:
        previous = ledger.summary(key).get("attempted_at")
        result = trial_rewards.attempt_trial(ledger, key, headers)
        if ledger.summary(key).get("attempted_at") != previous:
            _log(f"[trial] 领取检查 | ok={result['ok']} | already={result['already']} | code={result['code']} | status={result['status']}")
    except Exception as error:
        _log(f"[trial] 领取失败（不影响余额同步）: {_network_error_text(error)}")


def _sync_credits(pool, ledger, entry, *, checkin, failed, claim_trial=True, expected_identity=None):
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
                trip = travel.perform(token, profile_for_headers(headers), can_write=can_travel)
                if not pool.apply_if_current(cm, generation, lambda: travel.remember(ledger, cid, trip)):
                    failed.add(cid)
                    return None
            except Exception as error:
                _sync_error(pool, ledger, entry, generation, "travel", error)
        if claim_trial:
            _sync_trial(headers)
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
    """只发布账号绑定的产品版本缓存；旧 root/profile 表没有可验证的所有者。"""
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
    """历史用量仅在定时/手动维护时同步；每账号独立快照，单账号失败只替换自身数据。"""
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
    """按当前启用账号的快照重建聚合视图；本轮失败的账号保留历史并列入 stale_accounts。

    窗口说明：凭据身份更换后，旧快照最多残留一个同步周期，随后被新账号的快照替换。"""
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
    # 本轮失败的启用账号即使没有任何历史快照也必须可见，否则不完整聚合被当成精确值
    for cred_id in stale:
        if cred_id in enabled:
            partial = True
            stale_out.append(Path(cred_id).name)
    # 无成功快照也发布完整性标记；fetched_at=0 使账务继续使用额度差回退。
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
    """串行维护并提交同代次结果；新凭据只触发额度和目录查询。"""
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
    """新凭据事件即时唤醒；失败退避重试，整轮维护仍按小时进行。"""
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
# 模型列表
# ---------------------------------------------------------------------------

# 兜底模型表：云端 /v3/config 同步失败时使用，取值对齐官方国内账号模型集
DEFAULT_MODELS = [
    "hy4-preview", "hy4-preview-x",
    "hy3", "hy3-x",
    "deepseek-v4-pro", "deepseek-v4-flash", "deepseek-v4.1-flash", "deepseek-v3-2-volc",
    "glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-5.1", "glm-5.0", "glm-5.0-turbo",
    "glm-5v-turbo", "glm-4.7", "glm-4.6", "glm-4.6v",
    "minimax-m3", "minimax-m2.7", "minimax-m2.5",
    "kimi-k3-1", "kimi-k2.7", "kimi-k2.6", "kimi-k2.5", "kimi-k2-thinking",
    "hunyuan-chat", "default",
    "auto",  # 网关侧调度别名：由后端自行选路
]


# 后端请求体里出现过的额外字段（透传时若客户端给了就保留）
PASSTHROUGH_BODY_KEYS = {
    "model", "messages", "tools", "tool_choice", "temperature",
    "max_tokens", "max_completion_tokens", "top_p", "stream",
    "stream_options", "stop", "presence_penalty", "frequency_penalty",
    "n", "response_format", "seed", "user", "reasoning_effort", "prompt_cache_key",
    "verbosity", "reasoning_summary", "parallel_tool_calls",
}

# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------

app = FastAPI(title="codebuddy2api", version=APP_VERSION)

# Anthropic 错误类型映射：按 https://platform.claude.com/docs/en/api/errors 成形
_ANTHROPIC_ERROR_TYPES = {
    "auth_error": "authentication_error",
    "rate_limit_error": "rate_limit_error",
    "invalid_request_error": "invalid_request_error",
    "not_found_error": "not_found_error",
    "upstream_error": "api_error",
}


@app.exception_handler(HTTPException)
async def _protocol_http_exception(request: Request, exc: HTTPException):
    """推理端点（/v1/*）的错误体按客户端协议成形；/admin 与其他路由保持 FastAPI 默认 detail 包装。"""
    path = request.url.path
    if not path.startswith("/v1/"):
        return await _default_http_exception_handler(request, exc)
    detail = exc.detail
    err = detail.get("error") if isinstance(detail, dict) else None
    if not isinstance(err, dict):
        err = {"message": str(detail), "type": "error"}
    message = str(err.get("message") or "")
    if path.startswith("/v1/messages"):
        # Anthropic：{"type": "error", "error": {...}}；上游业务 code 原样保留，客户端仍可识别 content_filter
        etype = _ANTHROPIC_ERROR_TYPES.get(str(err.get("type") or ""))
        if exc.status_code == 404:
            etype = "not_found_error"  # Anthropic 约定：404 恒为 not_found_error
        elif etype is None:
            etype = "api_error" if exc.status_code >= 500 else "invalid_request_error"
        error_obj = {**err, "type": etype, "message": message}  # code/param/image_count 等结构化字段原样保留
        return JSONResponse({"type": "error", "error": error_obj},
                            status_code=exc.status_code, headers=exc.headers)
    # OpenAI：顶层 error 对象，保留 param/code 等既有字段
    body = {"error": {**err, "message": message}}
    return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)
CONFIG: dict = {"api_key": "", "cred": None, "log_path": None, "ledger": None,
                "admin_csrf": True,     # 管理 Origin/CSRF 校验，仅允许启动配置关闭
                "models_remote": None,   # 国内站云端模型表（缓存或同步结果）
                "models_intl": None,     # 国际站云端模型表（仅当有国际凭证且有额度时对外暴露）
                "model_cache": None,     # ModelCatalogCache：按站点分组持久化，TTL 内不打云端
                "model_catalogs": {},   # 仅供展示的产品合并目录（选择器子集）
                "account_catalogs": None,  # 生产按账号指纹绑定；None 仅兼容无持久缓存的嵌入模式
                                           # 每项含 models（选择器子集）与 serves（账号根表候选）
                "auto_trial": False, "trial_ledger": None,
                "model_guard": True,     # 表外模型本地拦截，不转发上游
                "max_images": 16, "image_policy": "truncate",
                "max_request_bytes": 32 * 1024 * 1024, "log_body_limit": 65536,
                "max_inbound_bytes": 64 * 1024 * 1024,
                "max_collect_bytes": 8 * 1024 * 1024, "max_concurrent": 64,
                "failover_max": 0,     # 流式失败在第一个字节之前发生时可换凭证重放的最大次数
                "retry_write_timeout": False,  # 写请求体超时是否也算「上游没收下请求体」（默认否，见 --retry-write-timeout）
                "usage_daily": None,     # 官方用量聚合视图（日期×模型 credit），供 billing/usage 出 daily_costs
                "usage_daily_accounts": None,  # 按账号的用量快照；单账号失败不丢历史
                "credit_price_cny": None, "credit_price_usd": None, "usd_rate": None,
                "desensitize": False, "no_compact": False, "keep_tool_metadata": False}  # 单价 None=取 credits 模块默认

# 无感登录状态机（内存态；重启后未完成的登录需重新发起）
_OAUTH = auth_oauth.OAuthManager(user_agent=USER_AGENT)


# ---------------------------------------------------------------------------
# 日志（写文件）
# ---------------------------------------------------------------------------

_LOG_LOCK = threading.Lock()
LOG_MAX_BYTES = 50 * 1024 * 1024  # 单日志文件上限（超限轮转；单条巨行可能略超）
LOG_BACKUPS = 2                    # 轮转保留份数（log.1、log.2，最老丢弃）


def _log(msg: str):
    """写入脱敏有界日志，在同一把锁内检查大小与轮转。"""
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
        pass  # 日志失败不应影响主流程


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
    """返回 ((凭据管理器, 代次), headers)；无可用凭据返回 503，模型冷却返回 429。

    `tried` 里的凭证不再入选，供换凭证重放使用（见 `_routed_stream`）。
    """
    raw_key = session_key(payload)
    skey = f"{region}:{raw_key}" if raw_key and region is not None else raw_key
    skey = model_policy.sticky_scope(CONFIG, skey, model)
    pool = CONFIG.get("cred_pool")
    if pool is not None:
        picked = pool.headers_for(skey, model, region=region, with_generation=True, tried=tried)
        if picked is None:
            until = pool.model_cooldown_until(model, region=region)
            if until:
                t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(until))
                raise HTTPException(status_code=429, detail={"error": {
                    "message": f"模型 {model} 额度冷却中（全部凭证），预计 {t} 重置后恢复",
                    "type": "rate_limit_error"}})
            blocked = pool.model_block_until(model, region=region)
            if blocked:
                # 后端已明确回过「无此模型」：给 404 让客户端换模型，别再拿空回复编故事
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
    headers.update(_dynamic_request_headers(f"{profile}:{skey}" if skey else None))
    return cm, headers


def _route_chat(payload, body, rid, *, tried=()):
    """根据所选账号自动确定后端地域、产品及模型，不改变客户端地址。"""
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
    """上游 200 即该后端认这个模型：解除 (后端, 模型) 避让。"""
    pool = CONFIG.get("cred_pool")
    if pool is not None and cred is not None and model:
        cm = cred[0] if isinstance(cred, tuple) else cred
        pool.note_model_ok(cm, model)


def _note_cred_status(cred, status: int, model: str | None = None, raw: bytes = b""):
    """后端 401/403 熔断该凭证；429 按 (凭证,模型) 冷却；11102 按 (后端,模型) 避让。

    黏性会话下次请求自动换绑/换后端。"""
    pool = CONFIG.get("cred_pool")
    if pool is not None and cred is not None:
        cm, generation = cred if isinstance(cred, tuple) else (cred, None)
        pool.note_status(cm, status, model=model, raw=raw, generation=generation)

@app.get("/health")
def health():
    """公开存活检查，不访问或暴露凭证池。"""
    return {"status": "ok"}


@app.get("/admin/credentials")
def admin_list_credentials(authorization: Optional[str] = Header(default=None),
                           x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """凭证池状态：账号、过期时间、健康度、黏绑会话数。"""
    _check_admin_auth(authorization, x_api_key)
    pool = CONFIG.get("cred_pool")
    if CONFIG.get("management") is not None:
        return {"credentials": CONFIG["management"].admin_credential_inventory()}
    return {"credentials": pool.snapshot() if pool else []}


class CredentialConflictError(CredentialFileError):
    """同一账号已由其他凭据文件持有。"""


def _store_credential(directory: Path, name: str, content: bytes, uid: str, *, replace_identity=True,
                      replace_existing=True) -> Path:
    """导入和登录共用的写入临界区，与后台刷新及独立 CLI 协调。"""
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
    """从允许目录导入已校验的凭据，原子更新并热加入池。"""
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
        # 与上传路径一致：落盘前折叠 token 别名为官方字段名
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
    """按文件名移除池内凭据（会删除该 *.info 文件）。"""
    _check_admin_auth(authorization, x_api_key)
    pool = CONFIG.get("cred_pool")
    if CONFIG.get("management") is not None:
        CONFIG["management"].admin_delete_guard(os.path.basename(name))
    if pool is None or not pool.remove_file(os.path.basename(name)):
        raise HTTPException(status_code=404, detail={"error": {"message": f"凭据不在池中: {name}", "type": "invalid_request_error"}})
    return {"removed": os.path.basename(name)}



def _save_oauth_credential(cred: dict) -> Path:
    """按产品/账号/租户更新；另一产品同 UID 的文件不可被 OAuth 覆盖。"""
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
    """无感登录第一步：申请 OAuth state + 授权链接（浏览器扫码即可，无需桌面端）。site=cn|intl。"""
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
    """无感登录第二步：轮询授权结果；完成后自动入库并热加入凭证池（同 uid 覆盖更新）。"""
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
    """各凭证积分余额/分段过期时间/今日签到状态（CreditLedger 缓存快照）。"""
    _check_admin_auth(authorization, x_api_key)
    ledger = CONFIG.get("ledger")
    return {"credits": ledger.snapshot() if ledger else {}}


@app.get("/admin/model-blocks")
def admin_model_blocks(authorization: Optional[str] = Header(default=None),
                       x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """(后端, 模型) 避让表：官方回过 service info not found 的组合，到期自动放行重试。"""
    _check_admin_auth(authorization, x_api_key)
    pool = CONFIG.get("cred_pool")
    return {"model_blocks": pool.model_blocks_detail() if pool is not None else []}


@app.post("/admin/checkin")
def admin_checkin(authorization: Optional[str] = Header(default=None),
                  x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """手动签到（按日幂等），余额和用量由独立同步操作更新。"""
    _check_admin_auth(authorization, x_api_key)
    return _admin_credential_action("checkin")


def _admin_credential_action(action, identity=None):
    from app.credential_actions import run
    return run(sys.modules[__name__], action, identity)


@app.post("/admin/sync")
def admin_sync(authorization: Optional[str] = Header(default=None),
               x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_admin_auth(authorization, x_api_key)
    return _admin_credential_action("sync")


@app.post("/admin/credentials/{identity}/{action}")
def admin_credential_action(identity: str, action: str,
                            authorization: Optional[str] = Header(default=None),
                            x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_admin_auth(authorization, x_api_key)
    return _admin_credential_action(action, identity)


# ---------------------------------------------------------------------------
# OpenAI 兼容余额端点：Credits 按订阅摊算口径折算为美元
# ---------------------------------------------------------------------------

def _billing_totals() -> dict:
    """余额快照：国内/国际分组折算（两站积分独立且单价不同）。

    已用量优先取官方明细的实际扣减，明细缺失时回退「总额度 − 剩余」。"""
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
             else float(g.get("used_by_quota") or 0))  # 该组无明细则回退额度差
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
            # 任一端数据不完整（积分分页到顶 / 用量到顶 / 账号同步失败）时对外可见
            "partial": bool(agg.get("partial") or cache.get("partial")),
            "groups": groups_out, "by_day": cache.get("by_day") or {}}


@app.get("/v1/dashboard/billing/subscription")
def billing_subscription(authorization: Optional[str] = Header(default=None),
                         x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """OpenAI 订阅端点外观：hard_limit_usd 为总额度折算，故余额 = hard_limit_usd − usage/100。"""
    _check_auth(authorization, x_api_key)
    t = _billing_totals()
    limit = t["quota_usd"]
    return {
        "object": "billing_subscription",
        "has_payment_method": True, "canceled": False, "canceled_at": None, "delinquent": None,
        # access_until 取池内最早积分过期时间：过期即额度归零的保守表达
        "access_until": int(t["soonest_expiry"] or (time.time() + 30 * 86400)),
        "soft_limit": int(limit * 100), "hard_limit": int(limit * 100),
        "soft_limit_usd": limit, "hard_limit_usd": limit, "system_hard_limit_usd": limit,
        "plan": {"title": f"CodeBuddy Credits (CN {t['price_cny']:g} CNY/credit · "
                                        f"INTL {t['price_usd']:g} USD/credit)"},
        # 扩展字段：直接给出总计金额与各站拆分，未知字段的客户端会忽略
        "codebuddy_credits_remaining": t["remaining"],
        "codebuddy_credits_used": t["used"],
        "codebuddy_balance_usd": t["remaining_usd"],
        "codebuddy_balance_cny": t["remaining_cny"],
        "codebuddy_sites": t["groups"],
        # 余额/用量不完整（分页到顶或账号同步失败）时调用方必须能看到
        "codebuddy_partial": t["partial"],
        **({"codebuddy_stale_accounts": stale} if (stale := (CONFIG.get("usage_daily") or {}).get("stale_accounts")) else {}),
    }


@app.get("/v1/dashboard/billing/usage")
def billing_usage(start_date: Optional[str] = None, end_date: Optional[str] = None,
                  authorization: Optional[str] = Header(default=None),
                  x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """OpenAI 用量端点：total_usage 单位美分；daily_costs 为官方明细按天×模型聚合（最近 30 天）。"""
    _check_auth(authorization, x_api_key)
    t = _billing_totals()
    # 逐站逐日按本站单价折算后再合并：两站单价不同，统一平均价会让每天/每模型的金额失真。
    # Σdaily 与 total_usage 都由同一组分站用量算出，恒等关系保持不变。
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
    if start_date or end_date:  # 指定区间时按区间明细求和
        total_cents = round(sum(sum(i["cost"] for i in d["line_items"]) for d in daily), 2)
    else:                      # 全量口径与 subscription 构成余额恒等式
        total_cents = round(t["used_usd"] * 100, 2)
    out = {"object": "list", "total_usage": total_cents, "daily_costs": daily}
    if t.get("partial"):
        out["partial"] = True
    if detail.get("stale_accounts"):
        out["stale_accounts"] = detail["stale_accounts"]
    return out


# 对外模型表：云端 /v3/config 同步结果优先，DEFAULT_MODELS 兜底补充
_MODEL_TABLE_TTL = 60.0   # 快照复用秒数，避免每请求重建
_model_table_cache: dict = {}


def invalidate_model_table() -> None:
    """模型表变更后作废快照缓存。"""
    global _model_table_cache
    _model_table_cache = {}


def _catalog_for(profile: str, scope: str = "models"):
    """该 profile 的模型目录；scope 含义见 _account_scope。"""
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
    """models 取选择器；serves 合并根表候选，同名保留选择器元数据。"""
    picker = account.get("models")
    if scope == "models" or picker is None:
        return picker
    seen = {item.get("id") for item in picker}
    # 子集优先：同名条目保留 agent 里的那份元数据，根表只负责补名字。
    return picker + [item for item in account.get("serves") or [] if item.get("id") not in seen]


def _models_for_profile(profile: str, configured=None, *, scope: str = "models") -> list[dict]:
    models = _catalog_for(profile, scope)
    if models is None:
        # 只有旧式国内 CLI 单产品部署保留静态兜底，不把未知表借给 WorkBuddy。
        configured = _configured_profiles(profile_region(profile)) if configured is None else configured
        return ([{"id": name, "supportsToolCall": True} for name in DEFAULT_MODELS]
                if CONFIG.get("model_cache") is None and CONFIG.get("account_catalogs") is None
                and profile == "cn-cli" and configured <= {"cn-cli"} else [])
    return _usable_models(models)


def _upstream_model(model: str | None, profile: str) -> str | None:
    return "default-model" if model == "auto" and profile_region(profile) == "intl" else model


def _free_multiplier(credits) -> bool:
    """目录 credits 倍率是否为 0（官方对当前账号声明的零计费标记）。"""
    if not isinstance(credits, str):
        return False
    match = re.fullmatch(r"x\s*0(?:\.0+)?\s*(?:credits?)?", credits.strip(), re.IGNORECASE)
    return match is not None


def _multiplier_value(credits):
    """解析官方倍率字符串为数值；无倍率或格式未知返回 None。"""
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
    """该账号目录是否把此模型声明为 x0.00；名单里没有该模型时不算免费。"""
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
        # WorkBuddy 有真实 Auto 时固定用它；只有 CLI 的旧部署保留 auto，不混轮询两种默认策略。
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
    """合并账号可用的模型；客户端不用按地域改变请求地址。"""
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
                # 零余额账号退出付费模型：不发布付费项，仅保留自身声明的零倍率模型。
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
                # 该产品全部账号余额归零时，只发布自身目录声明的零倍率模型。
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
    """模型表（含倍率）：{id, credits, credits_by_profile}；credits 取各来源最小值。"""
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
                return  # 零余额账号不参与付费模型的倍率展示
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
    """stream 缺省为 False（OpenAI/Anthropic 协议默认非流式）；非布尔类型显式 400。
    目标客户端（Codex CLI / Claude Code）均显式发送 stream:true，不受影响。"""
    value = payload.get("stream", False)
    if not isinstance(value, bool):
        raise HTTPException(status_code=400, detail={"error": {
            "message": "stream must be a boolean", "type": "invalid_request_error", "param": "stream"}})
    return value


def _prepare_payload(payload, field="messages") -> dict:
    """先处理整次请求的图片，再进行适配、日志记录和凭证选取。"""
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
    """上游只接收字符串；点名调用等价于仅提供该工具并设 required。"""
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


def _prepare_chat_body(body: dict, *, region=None) -> dict:
    """统一模型、首条 system、后端流式参数、脱敏与体积预算。"""
    body = dict(body)
    body["model"] = model_policy.resolve(CONFIG, body.get("model", "auto"))
    guard_model(body["model"], region=region, resolved=True)
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages or any(not isinstance(message, dict) for message in messages):
        raise HTTPException(status_code=400, detail={"error": {
            "message": "messages must be a non-empty array of objects", "type": "invalid_request_error"}})
    # Upstream compatibility: gateways such as copilot.tencent.com and
    # workbuddy.ai reject the "developer" role with 11128 "Illegal API
    # invocation from an unapproved channel"; official clients only send
    # "system". Normalize the role, keep the content, and do not mutate the
    # caller's message dicts.
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
    """校验并返回上游 JSON 字节数，不截断文本或工具参数。"""
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
    """表外模型本地拒绝；自动路由只考虑各账号明确支持的模型。"""
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
    # 凭证在构造后端 headers 时按会话黏绑选取

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})

    payload = _prepare_payload(payload)
    # 聚合路径无法保持多候选独立：n 缺省或恰为 1，否则显式拒绝而非拼接答案
    n_value = payload.get("n")
    if n_value is not None and not (isinstance(n_value, int) and not isinstance(n_value, bool) and n_value == 1):
        raise HTTPException(status_code=400, detail={"error": {
            "message": "only n=1 is supported: multiple candidates would be merged into one answer",
            "type": "invalid_request_error", "param": "n"}})
    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {"message": "messages is required", "type": "invalid_request_error"}})

    # 构造后端 body：只透传已知的合法字段
    client_wants_stream = _client_wants_stream(payload)
    body = {k: payload[k] for k in PASSTHROUGH_BODY_KEYS if k in payload}
    body = _prepare_chat_body(body)

    # 日志：请求摘要
    model_name = payload.get("model", "auto")
    tool_names = [t.get("function", {}).get("name") for t in (payload.get("tools") or [])
                  if isinstance(t, dict)]
    last_user = _last_user_text(messages)
    rid = os.urandom(4).hex()
    _log(f"[{rid}] ▶ REQUEST {model_name} | stream={client_wants_stream} | msgs={len(messages)}"
         + (f" | tools={tool_names}" if tool_names else "")
         + (f" | last_user={_truncate(last_user, 60)!r}" if last_user else ""))
    # 凭据选择/到期刷新持线程锁与文件锁并可能同步访问网络：放到受限线程池，不占事件循环
    prepared = body        # 改写前的规范请求体，换凭证重放按它判定绑定
    body, cred, headers, url = await run_in_threadpool(_route_chat, payload, body, rid)
    _log_json(f"[{rid}] REQUEST BODY (发往后端，预览)", body)
    t0 = time.time()

    if client_wants_stream:
        def attempt(routed, cred, headers, url):
            return _stream_upstream(url, headers, routed, model_name, t0, rid, cred=cred)
        return _routed_stream(payload, prepared, model_name, rid, t0, attempt,
                              body, cred, headers, url)

    # 非流式：后端只支持流式，这里把后端 SSE 聚合成单个 chat.completion 响应
    async def fetch(routed, cred, headers, url):
        return await _fetch_checked_chat(url, headers, routed, model_name, rid, cred,
                                         filter_retry=True)
    collected = await _routed_fetch(payload, prepared, model_name, rid, t0, fetch,
                                    body, cred, headers, url)
    _log_finish(model_name, t0, collected, rid)
    if CONFIG.get("control_store") is not None:
        collected = {**collected, "model": model_name}
    return JSONResponse(content=collected)


def _last_user_text(messages: list) -> str:
    """取最后一条 user 消息的文本，用于日志预览。"""
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
    """记录完成请求的耗时、结束原因、用量、工具调用和有界响应预览。"""
    elapsed = time.time() - t0
    prefix = f"[{rid}] " if rid else ""
    choice = (result.get("choices") or [{}])[0]
    finish = choice.get("finish_reason")
    msg = choice.get("message") or {}
    detector = ContentFilterDetector()
    detector.feed(msg, finish)
    if detector.detected:
        return  # 审核只记录分类，不把可能回显输入的正文/思考写入预览。
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
    """使用公共聚合器保留正文、思考和工具调用，并验证流完整性。"""
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
    """校验聚合后的 tool_calls：name 属于已声明工具，arguments 是含 JSON 对象的字符串。"""
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
        # 解析成功不等于正确：null/[]/42/"text" 都不是合法工具参数
        # 名称核对只在请求确实声明了工具时进行；未声明工具的请求收到的工具调用交由客户端裁决
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
    """文本路径与异步流路径使用同一聚合器。"""
    accumulator = ChatSSEAccumulator(max_collect_bytes=CONFIG.get("max_collect_bytes", 0))
    for line in text.splitlines():
        accumulator.feed_line(line)
    return accumulator.result()


def _chat_result_to_sse_lines(m: dict) -> list[str]:
    """把聚合结果伪流式化为标准 OpenAI SSE 文本行（chat 直接转发，anthropic 喂转换器）；reasoning 先于正文重放。"""
    content = m.get("content") or ""
    reasoning = m.get("reasoning_content") or ""
    tcs = m.get("tool_calls") or []
    finish = m.get("finish_reason") or "stop"
    model = m.get("model")
    # 一次响应的所有 chunk 共享稳定的 completion 标识，严格客户端可按契约关联事件
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
    def retry(error):
        """同一连接上的底层重放：换凭证那条日志到不了这里，风险标记得自己带上。

        建连失败/建连超时上游手里没有正文，标出来反而是噪音；写超时按 opt-in 参与重放时，
        「正文没写完」证不了上游没动过账，所以必须和换凭证重放同一口径标注（评审 P2）。
        `stage` 分开记，审计里能一眼看出是哪一类重放。
        """
        timeout_on_write = isinstance(error, WRITE_TIMEOUT_TRANSPORT)
        observe_attempt("write_timeout_retry" if timeout_on_write else "connect_retry",
                        error_code=type(error).__name__,
                        duration_ms=(time.monotonic() - started) * 1000)
        _log(f"[{rid}] {'写超时重放' if timeout_on_write else '建连失败'}，重试 1/1 | {model_name}"
             f" | {_network_error_text(error)}{_replay_cost_note(error)}")
    try:
        async with open_backend_stream(url, headers, body, read_timeout=timeout, on_retry=retry,
                                       retry_write_timeout=bool(CONFIG.get("retry_write_timeout"))) as response:
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


def _check_upstream_status(status, raw, cred, model):
    if status != 200:
        if not is_filter_error(raw):
            _note_cred_status(cred, status, model=model, raw=raw)
        raise UpstreamHTTPError(status, raw)


def _upstream_failure(error, model_name, t0, rid):
    """统一失败日志与错误体，协议包装由各端点负责。"""
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


async def _fetch_checked_chat(url, headers, body, model_name, rid, cred=None, *, filter_retry=False):
    """统一聚合与校验；非流式纯审核拒绝最多压缩兜底一次，网络错误不重放。"""
    tool_attempt = 0
    filter_retried = False
    while True:
        accumulator = ChatSSEAccumulator(max_collect_bytes=CONFIG.get("max_collect_bytes", 0))
        rejection = None
        async with _backend_stream(url, headers, body, rid=rid, model_name=model_name) as response:
            if response.status_code != 200:
                _check_upstream_status(response.status_code, await read_bounded_error(response), cred, body.get("model"))
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
        # 审核拒绝不是工具损坏，不因 required 工具选择而重复生成。
        budget = CONFIG.get("tool_call_max_retry", _TOOL_CALL_MAX_RETRY)
        if detector.detected or not body.get("tools") or tool_attempt >= budget:
            if not detector.detected and body.get("tools"):
                # 耗尽预算的末次生成同样消耗额度：记入 attempts 再报错
                exhausted = result.get("usage") or {}
                observe_attempt("tool_args_exhausted", attempt=tool_attempt, max_attempts=budget,
                                total_tokens=exhausted.get("total_tokens"))
            raise UpstreamResponseError(502, b"Invalid upstream tool_calls after retries")
        tool_attempt += 1
        # 被丢弃的这次生成也是真实消耗：连同序号记进 attempts，账务不再只看见最后一次
        discarded = result.get("usage") or {}
        observe_attempt("tool_args_retry", attempt=tool_attempt, max_attempts=budget,
                        total_tokens=discarded.get("total_tokens"))
        _log(f"[{rid}] tool_calls 损坏，重试 {tool_attempt}/{budget} | {model_name}")

async def _chat_sse_lines(url, headers, body, model_name, t0, rid, cred=None, *, aggregate=False):
    """提供公共 Chat SSE 行流；流式请求不做审核重试，正文检测缓冲有界。"""
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
            _check_upstream_status(response.status_code, await read_bounded_error(response), cred, body.get("model"))
        else:
            _note_cred_model_ok(cred, body.get("model"))
        async for line in response.aiter_lines():
            tracker.feed_line(line)
            if tracker.done or tracker.finish_reason:
                tracker.result()  # 先验证，再向客户端发出成功终止帧。
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
            raise      # 一个字节都没发出去：交给端点还原成真实状态码，别把失败写成 200
        status, raw = _upstream_failure(error, model_name, t0, rid)
        yield _err_event(raw, status)




def _err_event(msg: bytes, status: int) -> bytes:
    chunk = {"error": {"message": sanitize_log_text(msg.decode("utf-8", "replace"), 512),
                       "type": "upstream_error", "code": status}}
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")


def _cred_manager(cred):
    """凭证统一是 (管理器, 代次)；兼容裸管理器（`_cred_for` 的单凭证回退分支）。"""
    return cred[0] if isinstance(cred, tuple) else cred


# 可换凭证重放的上游 HTTP 状态：限流、认证、网关抖动。400/404/413 是确定性拒绝，换账号
# 也一样，不在其中。
FAILOVER_CODES = frozenset({401, 403, 429, 502, 503, 504})
# 上游手里没有任何正文的传输失败（建连阶段就失败），重放零风险。
REPLAYABLE_TRANSPORT = (httpx.ConnectError, httpx.ConnectTimeout)
# 写超时：正文没写完是确定的，上游有没有按已收到的半截正文动过账则观察不到，
# 因此只有 `--retry-write-timeout` 打开后才参与重放（两层重放都受这个开关约束）。
WRITE_TIMEOUT_TRANSPORT = (httpx.WriteTimeout,)
# 上游网关在拿到后端答复之前就把错误抛回来的状态：后端那侧可能已经处理完并计费。仍然重放
# （理由见 _failover_safe），但要如实标出来，便于事后拿官方账本核对。
POSSIBLY_CHARGED_CODES = frozenset({502, 504})


def _replay_cost_note(error) -> str:
    """重放日志里的代价标记：只给「可能已经付费」的那一类加，别把 429 也说成有风险。"""
    if isinstance(error, UpstreamHTTPError) and error.status in POSSIBLY_CHARGED_CODES:
        return " | 上游可能已处理该请求"
    if isinstance(error, WRITE_TIMEOUT_TRANSPORT):
        return " | 上游可能已处理该请求（正文未写完）"
    return ""


def _failover_safe(error, raw=b"") -> bool:
    """这次失败能不能换账号重放：只认「上游没收下请求体」和「上游用 HTTP 状态码拒绝」。

    三条硬边界：内容审核拒绝不切号重放（那是模型的真实答复，换账号只会再撞一次同一堵墙，
    还白烧一次额度）；聚合器从 200 响应体里合成的 502（空流、坏 SSE、已开流后断连）不重放，
    因为上游已经回了 200、可能已经计费，而且那时状态码还收得回来；写超时默认也不重放，
    要显式 `--retry-write-timeout`。真正的重放窗口由 `open_backend_stream` 的 `opened` 标记
    与 `_preflight_stream` 守住。

    为什么 502/504 这类「上游可能已经处理并计费」的失败仍然重放：这类失败对下游是**彻底
    失败**——连响应头都没有，更没有可用的结果。不重放并不能把已经花掉的额度退回来，只是把
    一次已经付出的请求换成一段静默断掉的会话。所以取舍不是「省钱 vs 花钱」，而是「花一次已
    付的学费 vs 花两次并给出结果」。代价因此被严格夹住：默认 `--failover-max=0` 完全关闭，
    开启后每请求最多多打 N 次，且这类重放在日志里由 `_replay_cost_note()` 单独标注，可事后
    按官方用量明细核对。
    """
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
    """流式预取阶段的失败：状态码、错误体，以及原始异常（重放判定要看它是什么类型）。"""

    def __init__(self, status, raw, error=None):
        self.status = status
        self.raw = raw
        self.error = error
        super().__init__(f"stream failed before first byte (HTTP {status})")


# 断连收尾的等法：轮数而非墙上时间做上界。被反复取消时每次 await 都会立刻抛回来，用时间做
# 上界就变成忙等；100 轮足够走完一次正常的关闭（实测个位数轮次），走完不成就交给后台。
TEARDOWN_GRACE_CYCLES = 100
TEARDOWN_POLL_SECONDS = 0.01


def _drain_teardown(future) -> None:
    """后台收尾任务的异常只取走、不重抛：它跑在没人再取消它的任务里，最终会做完。"""
    if not future.cancelled():
        future.exception()


async def _teardown_finished(task) -> None:
    """尽量当场等收尾任务结束；等不到就挂个回调让它后台做完，绝不因此拖住取消本身。

    为什么不能老实 `await task`：下游断连时 anyio 的取消作用域**每个事件循环周期**重投一次
    取消（`_deliver_cancellation` 用 `call_soon` 自循环），当前任务里的任何 await 都会被反复
    打断。收尾因此放在独立任务里 —— 它不属于那个作用域，没人再取消它 —— 这里只是尽量把结果
    等成同步的，等不到也不影响它最终跑完。
    """
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
    """取生成器的第一段输出，但把「我们的等待」和「生成器自己的收尾」分开放。

    直接在当前任务里 `await agen.__anext__()` 有个实测问题：断连的取消打在生成器帧内部的
    await 上，帧自己的 `finally` 做到一半就被反复投进来的取消打断 —— `httpx` 正是在那里关
    连接，于是清理根本跑不完，连接留到读超时。放进子任务之后，外层取消打断的是我们的
    `await`，子任务只被取消一次，它的 `finally` 能自己走完。

    取消语义下这一轮已经作废，所以子任务的结果不取；异常交给 `_teardown_finished` 收尾时取走。
    """
    task = asyncio.ensure_future(agen.__anext__())
    try:
        return await asyncio.shield(task)
    except BaseException:
        task.cancel()
        await _teardown_finished(task)
        raise


async def _stream_segments(agen):
    """逐段读上游，语义等同 `async for chunk in agen`，但每一段都可被干净打断。

    复用 `_first_segment`：断连落在「两段之间」还是「正等下一段」都无所谓，生成器自己的
    `finally` 都能走完。
    """
    while True:
        try:
            yield await _first_segment(agen)
        except StopAsyncIteration:
            return


async def _preflight_stream(agen, model_name, t0, rid):
    """取到第一段输出之后再决定怎么回 200。

    `StreamingResponse` 一旦被迭代就把响应头发出去，而打上游发生在生成器里面 —— 于是上游的
    429、建连/写超时乃至审核拒绝，在流式下全都只能塞进 SSE 正文：客户端看到的是一个没有
    `choices`、也等不到 `response.completed` 的 200 流，被读成「模型答了个空」，会话静默
    结束，既不重试也不报错，审计里还记成一次成功。预取第一段之后，「一个字节都还没发出去」
    的失败可以还原成真实状态码，流式与非流式同一口径；真的中途断流才继续用带内 error 事件
    （那时状态码已经收不回来了）。
    """
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
    """显式收尾上游生成器，收尾跑在不受当前取消作用域影响的任务里。

    覆盖「取消落在两段之间、帧还停在 yield 上」这种情况：直接 `await agen.aclose()` 会被
    反复投递的取消打断在 `httpx` 关连接的半途。清理失败不改变已经定型的响应，所以只吞异常。
    """
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
    """把「预取第一段 + 必要的换凭证重放」放进 ASGI 生命周期里做的流式响应。

    预取不能就在端点里 `await`：`StreamingResponse.__call__` 是把 `stream_response` 和
    `listen_for_disconnect` 放进同一个任务组跑的，端点返回之前根本没有谁在消费
    `http.disconnect`。上游首段一旦卡住而客户端已经走了，这个 await 会一直挂到读超时，
    `ConcurrencyLimitMiddleware` 的名额也跟着占满 —— 表现为整个网关 503。搬进
    `stream_response` 之后，断连取消的就是我们此刻的 await，挂起的上游读被打断，生成器的
    finally 跑得完，名额立刻归还。

    响应头仍然等到确实有字节可发时才发出，所以「把失败还原成真实状态码」的能力不受影响：
    失败以 `HTTPException` 抛出，由 ExceptionMiddleware 成形（`/v1/*` 走协议化错误体），
    那一刻一个字节都还没出去。客户端中途断连则按普通流式断连处理 —— 取消穿出 `__call__`，
    和响应已经开始之后的行为一致；两种窗口里的读取都走 `_first_segment`，
    取消之后生成器的收尾仍然跑得完。
    """

    def __init__(self, plan):
        self._plan = plan        # async callable -> (上游生成器, 已预取的第一段)
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
    """预取第一段，失败就按策略换凭证重打；返回 (生成器, 首段)，全线失败才抛 `HTTPException`。

    重放只发生在「一个字节都没发给下游」的时候（`_preflight_stream` 保证了这点），所以下游
    看到的仍然是一次正常请求。`make(routed, cred, headers, url)` 每轮只建一个生成器。

    `canonical` 与 `routed` 必须分开：`_route_chat` 会把逻辑模型（`auto`）改写成该站点的
    默认模型再发出去，所以 `routed` 是「本轮的真实报文」，而重路由只能拿改写前的 `canonical`
    去问绑定规则 —— 否则第二轮查的是默认模型，客户端原来说的 `auto` 的账号/站点限制就丢了。
    """
    tried = []
    recovered = None
    while True:
        stream = make(routed, cred, headers, url)
        try:
            first = await _preflight_stream(stream, model_name, t0, rid)
        except _StreamFailure as failure:
            await _close_stream(stream)   # 本轮的上游已经终止，关掉只是兜底，不留半开的连接
            recovered = observe_failure_seq()   # 这一枪记的失败，才是重放有权撤销的那一次
            tried.append(cred)
            limit = _failover_limit()
            surface = HTTPException(status_code=failure.status,
                                    detail=_safe_err_raw(failure.raw, failure.status))
            if limit <= 0 or len(tried) > limit or not _failover_safe(failure.error, failure.raw):
                raise surface from None
            try:
                attempt = await run_in_threadpool(_route_chat, payload, canonical, rid,
                                                  tried={_cred_manager(item) for item in tried})
            except HTTPException:
                raise surface from None      # 换不出别的凭证，就如实回第一次的错
            if _cred_manager(attempt[1]) in {_cred_manager(item) for item in tried}:
                raise surface from None
            routed, cred, headers, url = attempt
            _log(f"[{rid}] ↻ 换凭证重放 {len(tried)}/{limit} | {model_name} | 上游 HTTP "
                 f"{failure.status} → {profile_for_headers(headers)}"
                 f"{_replay_cost_note(failure.error)}")
            continue                       # 换一个凭证，再预取一次
        except BaseException:
            # 下游断连（取消）或没预料到的错误：先把本轮上游收掉，再把异常原样交出去
            await _close_stream(stream)
            raise
        if tried:
            # 只撤销重放对应的那一次失败：序号对不上说明换到手的响应自己又记了新失败
            # （最典型是内容审核拒绝），那次失败要如实留在审计里。
            observe_recovery(recovered)   # 重放救回来的请求对下游是正常响应，不该记成失败
        return stream, first


def _routed_stream(payload, canonical, model_name, rid, t0, make, routed, cred, headers, url):
    """流式端点入口：返回一个把预取与重放留待 ASGI 生命周期内执行的响应。

    这里刻意「什么都不做就返回」：预取必须发生在 `_DeferredStreamResponse.stream_response`
    里，那里才有下游断连监听（见该类的说明）。
    """
    return _DeferredStreamResponse(
        lambda: _stream_plan(payload, canonical, model_name, rid, t0, make,
                             routed, cred, headers, url))


async def _routed_fetch(payload, canonical, model_name, rid, t0, fetch, routed, cred, headers, url):
    """非流式请求：失败时按同一策略换凭证重打（此时一个字节都还没回给下游）。

    `canonical` 同 `_routed_stream`：重路由用改写前的规范请求体，判定才落在客户端模型上。
    """
    tried = []
    recovered = None
    while True:
        try:
            collected = await fetch(routed, cred, headers, url)
            if tried:
                # 同 `_stream_plan`：聚合路径里 `_fetch_checked_chat` 会在返回前就记上审核拒绝，
                # 无差别撤销会把被拦截的请求写成一次成功。
                observe_recovery(recovered)   # 换凭证后成功的请求不该记成失败
            return collected
        except (httpx.HTTPError, UpstreamResponseError) as error:
            status, raw = _upstream_failure(error, model_name, t0, rid)
            recovered = observe_failure_seq()
            tried.append(cred)
            limit = _failover_limit()
            surface = HTTPException(status_code=status, detail=_safe_err_raw(raw, status))
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
# Responses API 端点（Codex CLI 兼容）
# ---------------------------------------------------------------------------

@app.post("/v1/responses")
async def create_response(request: Request,
                          authorization: Optional[str] = Header(default=None),
                          x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """OpenAI Responses API 兼容端点。

    Codex CLI 使用 Responses API（wire_api = "responses"）而非 Chat Completions。
    本端点接收 Responses 格式请求，转换为 Chat 格式发往后端，再将后端的 Chat SSE
    转换为 Responses 语义事件流返回。
    """
    _check_auth(authorization, x_api_key)

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})

    payload = _prepare_payload(payload, field="input")
    # 本网关不保留服务端响应状态：依赖服务端补全历史的字段必须显式拒绝而非静默开新对话
    for stateful in ("previous_response_id", "conversation"):
        if payload.get(stateful):
            raise HTTPException(status_code=400, detail={"error": {
                "message": f"{stateful} is not supported: this gateway keeps no server-side response state; resubmit the full input instead",
                "type": "invalid_request_error", "param": stateful}})
    # 转换请求：Responses → Chat
    try:
        chat_body = responses_request_to_chat(payload)
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"request conversion error: {e}", "type": "invalid_request_error"}})

    chat_body, projection_stats = project_responses_chat_body(
        chat_body, keep_tool_metadata=CONFIG.get("keep_tool_metadata", False))
    chat_body = _prepare_chat_body(chat_body)

    client_wants_stream = _client_wants_stream(payload)
    model_name = payload.get("model", "auto")
    rid = os.urandom(4).hex()
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
    # 同上：凭据选择/刷新是阻塞操作，移出事件循环
    prepared = chat_body        # 改写前的规范请求体，见 `_routed_stream`
    chat_body, cred, headers, url = await run_in_threadpool(_route_chat, payload, chat_body, rid)
    _log_json(f"[{rid}] RESPONSES → CHAT BODY (预览)", chat_body)
    t0 = time.time()

    if client_wants_stream:
        def attempt(routed, cred, headers, url):
            return _stream_responses(url, headers, routed, model_name, t0, rid, cred=cred)
        return _routed_stream(payload, prepared, model_name, rid, t0, attempt,
                              chat_body, cred, headers, url)

    return await _nonstream_adapted(url, headers, chat_body, model_name, t0, rid, cred,
                                    payload=payload, canonical=prepared)


async def _nonstream_adapted(url, headers, body, model_name, t0, rid, cred, *, anthropic=False,
                             payload=None, canonical=None):
    converter = (AnthropicStreamConverter(model=model_name) if anthropic else ResponsesStreamConverter(model=model_name, parallel_tool_calls=body.get("parallel_tool_calls", True)))

    async def fetch(routed, cred, headers, url):
        return await _fetch_checked_chat(url, headers, routed, model_name, rid, cred,
                                         filter_retry=True)
    try:
        collected = await _routed_fetch(payload, body if canonical is None else canonical,
                                        model_name, rid, t0, fetch, body, cred, headers, url)
        for line in _chat_result_to_sse_lines(_completion_to_merged(collected)):
            converter.feed_line(_public_sse_line(line, model_name))
        converter.finish()
    except (httpx.HTTPError, UpstreamResponseError) as error:
        status, raw = _upstream_failure(error, model_name, t0, rid)
        raise HTTPException(status_code=status, detail=_safe_err_raw(raw, status)) from None
    result = converter.get_nonstream_response()
    _log_finish(model_name, t0, collected, rid)
    return JSONResponse(content=result)


async def _stream_adapted(url, headers, body, model_name, t0, rid, cred=None, *, anthropic=False):
    """协议适配只处理事件映射，连接、聚合与错误边界共用。"""
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
            raise      # 一个字节都没发出去：交给端点还原成真实状态码，别把失败写成 200
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
# Anthropic Messages API 端点（Claude Code / CC Switch 兼容）
# ---------------------------------------------------------------------------

@app.post("/v1/messages")
async def create_message(request: Request,
                         authorization: Optional[str] = Header(default=None),
                         x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Anthropic Messages API 兼容端点。

    Claude Code / CC Switch 使用 Anthropic Messages API（POST /v1/messages）。
    本端点接收 Anthropic 格式请求，转换为 Chat 格式发往后端，再将后端的 Chat SSE
    转换为 Anthropic SSE 事件流返回。
    """
    _check_auth(authorization, x_api_key)

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})

    payload = _prepare_payload(payload)
    # 将 Anthropic 格式消息、工具规范在进入后端前统一转换为 OpenAI Chat 格式。
    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {"message": "messages is required", "type": "invalid_request_error"}})

    try:
        chat_body = anthropic_request_to_chat(payload)
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"request conversion error: {e}", "type": "invalid_request_error"}})

    chat_body = _prepare_chat_body(chat_body)
    model_name = payload.get("model", "auto")
    chat_messages = chat_body.get("messages", [])
    rid = os.urandom(4).hex()
    _log(f"[{rid}] ▶ ANTHROPIC {model_name} | msgs={len(chat_messages)} | anthropic_msgs={len(messages)}")
    # 同上：凭据选择/刷新是阻塞操作，移出事件循环
    prepared = chat_body        # 改写前的规范请求体，见 `_routed_stream`
    chat_body, cred, headers, url = await run_in_threadpool(_route_chat, payload, chat_body, rid)
    _log_json(f"[{rid}] ANTHROPIC → CHAT BODY (预览)", chat_body)
    t0 = time.time()

    if not _client_wants_stream(payload):
        return await _nonstream_adapted(url, headers, chat_body, model_name, t0, rid, cred,
                                        anthropic=True, payload=payload, canonical=prepared)

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
    """Anthropic token 计数端点：字符启发式估算（Claude Code 发送前据此做预算）。"""
    _check_auth(authorization, x_api_key)
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail={"error": {"message": "请求体必须是 JSON 对象", "type": "invalid_request_error"}})
    return {"input_tokens": _estimate_input_tokens(payload)}


def _estimate_input_tokens(payload: dict) -> int:
    """启发式估算：ASCII 约 4 字符 1 token，其余字符（如中文）按 1 token 计，每条消息加结构开销。
    只是预算参考，不是精确计数；客户端不得据此断言与上游计费一致。"""

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
                total += measure(message.get("content")) + 4  # 消息结构开销
    return total


# ---------------------------------------------------------------------------
# 启动
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
    """独立完成扫码与入库；凭据只写入自管目录，不经过本地 HTTP 接口。"""
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
        # 上游异常可能包含授权 URL 或响应正文，不向终端转储。
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
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
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
                    default=os.environ.get("CODEBUDDY2API_AUTO_TRIAL", "false"),
                    help="自动领取国际 WorkBuddy 一次性体验积分，默认关闭")
    args = ap.parse_args()
    if args.image_policy not in ("truncate", "error"):
        ap.error("CODEBUDDY2API_IMAGE_POLICY 必须为 truncate 或 error")
    if args.command == "login":
        return login(site=args.site, open_browser=not args.no_browser)

    for key in ("max_images", "image_policy", "max_request_bytes", "log_body_limit", "auto_trial",
                "tool_call_max_retry", "max_inbound_bytes", "max_collect_bytes", "max_concurrent",
                "failover_max", "retry_write_timeout"):
        CONFIG[key] = getattr(args, key)
    CONFIG["api_key"] = args.api_key
    CONFIG["desensitize"] = args.desensitize
    CONFIG["no_compact"] = args.no_compact
    CONFIG["credit_price_cny"] = args.credit_price_cny or None
    CONFIG["usd_rate"] = args.usd_rate or None
    CONFIG["credit_price_usd"] = args.credit_price_usd or None
    CONFIG["model_guard"] = not args.no_model_guard
    # --log 直接指定文件路径即开启；不传则不记
    CONFIG["log_path"] = args.log if args.log else os.environ.get("CODEBUDDY2API_LOG")
    from app import runtime_management
    runtime_management.initialize(sys.modules[__name__], args)
    # 持久化设置解析后再核对实际监听地址和生效 key，且必须早于凭据扫描、线程及监听。
    if (args.host not in ("127.0.0.1", "::1", "localhost") and not CONFIG.get("api_key")
            and os.environ.get("CODEBUDDY2API_ALLOW_OPEN_NOAUTH", "").lower() not in ("1", "true", "yes")):
        runtime_management.close(CONFIG)
        ap.error("非回环绑定且未设置 API key 会匿名开放推理额度；"
                 "请设置 CODEBUDDY2API_KEY，或确知风险后以 CODEBUDDY2API_ALLOW_OPEN_NOAUTH=true 显式放行")
    files = [Path(p) for p in args.auth_file]
    if not files:
        seed_credentials()  # 自管模式：启动时把桌面端缺失凭据复制进 auth/
    CONFIG["cred_pool"] = CredentialPool(files, scan=not files,
                                         blocks_path=managed_auth_dir() / "model-site-blocks.json")
    CONFIG["cred"] = CONFIG["cred_pool"].first()
    CONFIG["account_catalogs"] = {}  # 在任何维护线程/预检启动前关闭静态兜底。
    if credits_mod is not None:
        ledger = credits_mod.CreditLedger(managed_auth_dir() / "credits-ledger.json")
        CONFIG["ledger"] = ledger
        CONFIG["model_cache"] = credits_mod.ModelCatalogCache(
            managed_auth_dir() / "model-catalog.json", ttl=args.model_catalog_ttl)
        CONFIG["cred_pool"].set_ledger(ledger)  # 先验证持久余额所属身份，再发布目录（包括空表）。
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

    # 启动时写一条标记
    _log(f"==== converter 启动 ====")

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        runtime_management.close(CONFIG)


if __name__ == "__main__":
    sys.exit(main())
