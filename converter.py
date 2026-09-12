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
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
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
from app.credential_io import (CredentialFileError, read_import_file, atomic_write_credential,
                               credential_file_lock)
from app.upstream_io import ChatSSEAccumulator, UpstreamResponseError, open_backend_stream
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

    def __init__(self, paths: list[Path] | None = None, scan: bool = False):
        self._lock = threading.RLock()
        self._entries: list[dict] = []   # {id, cm, fail_until}
        self._sticky: "OrderedDict[str, tuple[str, float]]" = OrderedDict()
        self._model_fail: dict[tuple[str, str], float] = {}  # (cred_id, model) -> 冷却截止 epoch（429 模型级冷却）
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
            due = {cid for cid, deadline in self._sync_retry.items() if deadline <= time.monotonic()}
            self._sync_pending.update(due)
            ids = {entry["id"] for entry in self._entries} if all_entries else set(self._sync_pending)
            self._sync_pending.difference_update(ids)
            if not self._sync_pending:
                self._sync_event.clear()
            self._syncing.update(ids)
            return ids

    def end_sync(self, ids, failed=()):
        with self._lock:
            self._syncing.difference_update(ids)
            present = {entry["id"] for entry in self._entries}
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
            if entry is None:
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
        return time.time() >= e["fail_until"]

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

    def _eligible(self, entry, model, *, region=None, profile=None):
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
            models = account.get("models")
            if account.get("profile") != profile or models is None:
                return False
            usable = _usable_models(models)
            supported = any(item["id"] == _upstream_model(model, profile) for item in usable)
            cli_auto = model == "auto" and profile == "cn-cli" and bool(usable)
            # 关闭 guard 仅允许单产品的明确表外透传，不能把 A 的已知能力借给 B。
            declared = any(item["id"] == _upstream_model(model, profile)
                           for item in _models_for_profile(profile, configured))
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
            return _model_free(account.get("models"), model, profile)
        return _model_free(_models_for_profile(profile), model, profile)

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

    def _candidates(self, model: str | None, *, region=None) -> list[dict]:
        """可用凭证按（零计费优先, 快过期积分优先）排序；同级由调用方轮询。"""
        healthy = [entry for entry in self._entries if self._healthy(entry)
                   and self._eligible(entry, model, region=region) and self._model_healthy(entry, model)]
        if not healthy:
            return []
        # 目录倍率 x0.00 的同名模型排最前，其次快过期积分优先；无数据排最后。
        healthy.sort(key=lambda entry: (not self._model_free(entry, model), *self._expiry_rank(entry)))
        return healthy

    def pick(self, skey: str | None, model: str | None = None, *, region=None) -> CredentialManager | None:
        """按黏绑选凭证；未绑定/已失效则轮询取健康凭证并绑定。

        model 非空时跳过该模型 429 冷却中的凭证（黏性会话自动换绑）；
        全部凭证对该模型冷却时返回 None，由上层快速失败，不再打上游。
        候选优先零计费账号；黏绑账号被更好的来源替代时自动重绑。
        """
        self._rescan()  # 锁外扫描，reload/prune 各自取锁，避免死锁
        with self._lock:
            self._evict_sticky()
            candidates = self._candidates(model, region=region)
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

    def headers_for(self, skey: str | None, model: str | None = None, *, region=None, with_generation=False):
        """在发送前复核凭据代次和站点，避免重载竞态导致跨站调用。"""
        for _ in range(max(1, len(self._entries))):
            cm = self.pick(skey, model, region=region)
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
        _log(f"[cred] 凭证熔断 {CRED_COOLDOWN}s: {Path(cm.path).name} {reason}")

    def note_status(self, cm: CredentialManager | None, status: int,
                    model: str | None = None, raw: bytes = b"", *, generation=None):
        """401/403 熔断整个凭证；429 只冷却 (凭证,模型) 至配额重置时间，其他模型/凭证不受影响。"""
        if cm is None:
            return
        if status in (401, 403):
            self.cooldown(cm, reason=f"backend HTTP {status}", generation=generation)
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

    def refresh_due(self, margin_s: int = CRED_REFRESH_MARGIN, keepalive_s: int = CRED_KEEPALIVE_S):
        """按到期与保活条件刷新，失败退避只作用于发起操作时的凭据代次。"""
        with self._lock:
            entries = list(self._entries)
        now = time.time()
        for entry in entries:
            if now < entry.get("fail_until", 0.0):
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


def _sync_credits(pool, ledger, entry, *, checkin, failed):
    cm, cid = entry["cm"], entry["id"]
    generation = None
    try:
        with cm._lock:
            try:
                headers = cm.get_headers()
            finally:
                generation = cm._generation
        site = site_for_headers(headers)
        token, uid, domain = _bearer_token(headers), headers.get("X-User-Id", ""), headers.get("X-Domain", "")
        day = time.strftime("%Y-%m-%d")
        if checkin and not ledger.checkin_done(cid, day):
            try:
                result = credits_mod.daily_checkin(token, uid=uid, domain=domain)
                if not pool.apply_if_current(cm, generation, lambda: ledger.mark_checkin(
                        cid, day, result["ok"], result.get("code"), result.get("message", ""))):
                    failed.add(cid)
                    return None
                _log(f"[checkin] {Path(cid).name}: ok={result['ok']} already={result.get('already')} code={result.get('code')}")
            except Exception as error:
                _sync_error(pool, ledger, entry, generation, "checkin", error)
        _sync_trial(headers)
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
                accounts[identity] = {"profile": profile,
                                      "models": cache.models(key) if cache.age(key) is not None else None}
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
        key = catalog_cache_key(profile, identity)
        if cache.fresh(key) and not entry.get("catalog_dirty"):
            continue
        try:
            models = credits_mod.fetch_model_catalog(
                _bearer_token(headers), domain=headers.get("X-Domain", ""),
                uid=headers.get("X-User-Id", ""), enterprise_id=headers.get("X-Enterprise-Id", ""))
            def publish():
                cache.put(key, models)
                for current in pool._entries:
                    if current["cm"] is entry["cm"]:
                        current["catalog_dirty"] = False
            if pool.apply_if_current(entry["cm"], generation, publish):
                _log(f"[models] {profile} 模型表已刷新: {len(models)} 个")
            else:
                failed.add(entry["id"])
        except Exception as error:
            failed.add(entry["id"])
            _sync_error(pool, ledger, entry, generation, "models", error)
    _publish_model_cache()


def _sync_usage(pool):
    """历史用量仅在定时/手动维护时同步，入库唤醒不额外拉取历史。"""
    by_day, groups = {}, {}
    used, count, any_success = 0.0, 0, False
    for entry in pool.entries():
        try:
            cm = entry["cm"]
            with cm._lock:
                headers = cm.get_headers()
                generation = cm._generation
            site = site_for_headers(headers)
            usage = credits_mod.fetch_request_usage(_bearer_token(headers), uid=headers.get("X-User-Id", ""),
                                                    domain=headers.get("X-Domain", ""))
            def merge():
                nonlocal used, count, any_success
                any_success = True
                group = groups.setdefault(site, {"by_day": {}, "total_credits": 0.0, "requests": 0})
                for day, models in usage["by_day"].items():
                    total_day = by_day.setdefault(day, {})
                    site_day = group["by_day"].setdefault(day, {})
                    for model, credit in models.items():
                        total_day[model] = round(total_day.get(model, 0.0) + credit, 6)
                        site_day[model] = round(site_day.get(model, 0.0) + credit, 6)
                group["total_credits"] += usage["total_credits"]
                group["requests"] += usage["requests"]
                used += usage["total_credits"]
                count += usage["requests"]
            pool.apply_if_current(cm, generation, merge)
        except Exception as error:
            _log(f"[usage] {Path(entry['id']).name} 明细拉取失败: {_network_error_text(error)}")
    if any_success:
        for group in groups.values():
            group["total_credits"] = round(group["total_credits"], 2)
        CONFIG["usage_daily"] = {"by_day": by_day, "groups": groups, "total_credits": round(used, 2),
                                 "requests": count, "fetched_at": time.time()}
        _log(f"[usage] 明细已同步: {count} 请求 / {used:.2f} credits")


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
    "verbosity", "reasoning_summary",
}

# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------

app = FastAPI(title="codebuddy2api", version=APP_VERSION)
CONFIG: dict = {"api_key": "", "cred": None, "log_path": None, "ledger": None,
                "models_remote": None,   # 国内站云端模型表（缓存或同步结果）
                "models_intl": None,     # 国际站云端模型表（仅当有国际凭证且有额度时对外暴露）
                "model_cache": None,     # ModelCatalogCache：按站点分组持久化，TTL 内不打云端
                "model_catalogs": {},   # 仅供展示/guard 的产品合并目录
                "account_catalogs": None,  # 生产按账号指纹绑定；None 仅兼容无持久缓存的嵌入模式
                "auto_trial": False, "trial_ledger": None,
                "model_guard": True,     # 表外模型本地拦截，不转发上游
                "max_images": 16, "image_policy": "truncate",
                "max_request_bytes": 32 * 1024 * 1024, "log_body_limit": 65536,
                "usage_daily": None,     # 官方用量明细（日期×模型 credit），供 billing/usage 出 daily_costs
                "credit_price_cny": None, "credit_price_usd": None, "usd_rate": None,
                "desensitize": False, "no_compact": False}  # 单价 None=取 credits 模块默认

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
    key = CONFIG["api_key"]
    if not key:
        return
    token = ""
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    if not token and x_api_key:
        token = x_api_key
    if token != key:
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key", "type": "auth_error"}})


def _cred_for(payload: dict, model: str | None = None, *, region=None):
    """返回 ((凭据管理器, 代次), headers)；无可用凭据返回 503，模型冷却返回 429。"""
    raw_key = session_key(payload)
    skey = f"{region}:{raw_key}" if raw_key and region is not None else raw_key
    pool = CONFIG.get("cred_pool")
    if pool is not None:
        picked = pool.headers_for(skey, model, region=region, with_generation=True)
        if picked is None:
            until = pool.model_cooldown_until(model, region=region)
            if until:
                t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(until))
                raise HTTPException(status_code=429, detail={"error": {
                    "message": f"模型 {model} 额度冷却中（全部凭证），预计 {t} 重置后恢复",
                    "type": "rate_limit_error"}})
            raise HTTPException(status_code=503, headers={"Retry-After": "3" if _catalog_pending(region) else "30"},
                                detail={"error": {"message": "无可用凭证（未登录、目录/额度未就绪或全部熔断）",
                                                  "type": "auth_error"}})
        cm, headers = picked
    else:
        cm = CONFIG["cred"]
        if cm is None:
            raise HTTPException(status_code=503, detail={"error": {"message": "未找到登录凭据，请先在桌面端登录 CodeBuddy/WorkBuddy", "type": "auth_error"}})
        with cm._lock:
            headers = cm.get_headers()
            cm = (cm, cm._generation)
    profile = profile_for_headers(headers)
    if not _in_region(profile, region):
        raise HTTPException(status_code=503, detail={"error": {"message": "未找到指定地域凭据", "type": "auth_error"}})
    headers.update(_dynamic_request_headers(f"{profile}:{skey}" if skey else None))
    return cm, headers


def _route_chat(payload, body, rid):
    """根据所选账号自动确定后端地域、产品及模型，不改变客户端地址。"""
    cred, headers = _cred_for(payload, body.get("model"))
    profile = profile_for_headers(headers)
    routed_model = _upstream_model(body.get("model"), profile)
    if routed_model != body.get("model"):
        body = {**body, "model": routed_model}
        _guard_request_size(body)
    url = chat_url_for_headers(headers)
    _log(f"[{rid}] ROUTE | region={profile_region(profile)} | profile={profile} | model={routed_model} | url={url}")
    return body, cred, headers, url


def _note_cred_status(cred, status: int, model: str | None = None, raw: bytes = b""):
    """后端 401/403 熔断该凭证；429 按 (凭证,模型) 冷却。黏性会话下次请求自动换绑。"""
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
    _check_auth(authorization, x_api_key)
    pool = CONFIG.get("cred_pool")
    return {"credentials": pool.snapshot() if pool else []}


class CredentialConflictError(CredentialFileError):
    """同一账号已由其他凭据文件持有。"""


def _store_credential(directory: Path, name: str, content: bytes, uid: str, *, replace_identity=True) -> Path:
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
    _check_auth(authorization, x_api_key)
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
        cred_data = json.loads(content.decode("utf-8"))
        src_uid, verr = auth_oauth.validate_cred_data(cred_data)
        if verr:
            raise CredentialFileError("凭据格式或站点校验失败")
        if (not isinstance(cred_data.get("account") or {}, dict)
                or not isinstance(cred_data["auth"].get("expiresAt", 0), (int, float))):
            raise CredentialFileError("凭据账号或过期时间格式无效")
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
    _check_auth(authorization, x_api_key)
    pool = CONFIG.get("cred_pool")
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
    _check_auth(authorization, x_api_key)
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
    _check_auth(authorization, x_api_key)
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
    _check_auth(authorization, x_api_key)
    ledger = CONFIG.get("ledger")
    return {"credits": ledger.snapshot() if ledger else {}}


@app.post("/admin/checkin")
def admin_checkin(authorization: Optional[str] = Header(default=None),
                  x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """手动触发一轮签到 + 积分刷新（签到按日幂等，已签则只刷积分）。"""
    _check_auth(authorization, x_api_key)
    pool, ledger = CONFIG.get("cred_pool"), CONFIG.get("ledger")
    if pool is None or ledger is None:
        raise HTTPException(status_code=503, detail={"error": {"message": "签到调度未启用", "type": "invalid_request_error"}})
    _housekeep_once(pool, ledger)
    return {"credits": ledger.snapshot()}


# ---------------------------------------------------------------------------
# OpenAI 兼容余额端点：Credits 按订阅摊算口径折算为美元
# ---------------------------------------------------------------------------

def _billing_totals() -> dict:
    """余额快照：国内/国际分组折算（两站积分独立且单价不同）。

    已用量优先取官方明细的实际扣减，明细缺失时回退「总额度 − 剩余」。"""
    empty_grp = {"remaining": 0.0, "used_by_quota": 0.0, "soonest_expiry": None}
    ledger = CONFIG.get("ledger")
    snap = ledger.snapshot() if ledger else {}
    price_cny = CONFIG.get("credit_price_cny") or (credits_mod.CREDIT_PRICE_CNY if credits_mod else 0.014)
    price_usd = CONFIG.get("credit_price_usd") or (credits_mod.CREDIT_PRICE_USD if credits_mod else 0.03)
    rate = CONFIG.get("usd_rate") or (credits_mod.USD_RATE_CNY if credits_mod else 7.15)
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
    }


@app.get("/v1/dashboard/billing/usage")
def billing_usage(start_date: Optional[str] = None, end_date: Optional[str] = None,
                  authorization: Optional[str] = Header(default=None),
                  x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """OpenAI 用量端点：total_usage 单位美分；daily_costs 为官方明细按天×模型聚合（最近 30 天）。"""
    _check_auth(authorization, x_api_key)
    t = _billing_totals()
    # 每 Credit 美分单价：按各站实际用量加权（保证 Σdaily 与 total_usage 一致）
    cents_per_credit = ((t["used_usd"] * 100 / t["used"]) if t["used"]
                        else t["price_cny"] / t["rate"] * 100)
    daily = []
    for day in sorted(t["by_day"]):
        if start_date and day < start_date:
            continue
        if end_date and day > end_date:
            continue
        items = [{"name": m, "cost": round(c * cents_per_credit, 4)}
                 for m, c in sorted(t["by_day"][day].items()) if c > 0]
        try:
            ts = int(time.mktime(time.strptime(day, "%Y-%m-%d")))
        except ValueError:
            ts = 0
        daily.append({"timestamp": ts, "line_items": items})
    if start_date or end_date:  # 指定区间时按区间明细求和
        total_cents = round(sum(sum(i["cost"] for i in d["line_items"]) for d in daily), 2)
    else:                      # 全量口径与 subscription 构成余额恒等式
        total_cents = round(t["used_usd"] * 100, 2)
    return {"object": "list", "total_usage": total_cents, "daily_costs": daily}


# 对外模型表：云端 /v3/config 同步结果优先，DEFAULT_MODELS 兜底补充
_MODEL_TABLE_TTL = 60.0   # 快照复用秒数，避免每请求重建
_model_table_cache: dict = {}


def invalidate_model_table() -> None:
    """模型表变更后作废快照缓存。"""
    global _model_table_cache
    _model_table_cache = {}


def _catalog_for(profile: str):
    accounts = CONFIG.get("account_catalogs")
    if accounts is not None or CONFIG.get("model_cache") is not None:
        pool = CONFIG.get("cred_pool")
        models = None
        for entry in pool.entries() if pool is not None else []:
            if entry.get("profile") != profile:
                continue
            account = (accounts or {}).get(entry.get("account_key")) or {}
            if account.get("profile") == profile and account.get("models") is not None:
                if models is None:
                    models = []
                models.extend(account["models"])
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
                and _in_region(profile, region)}
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


def _models_for_profile(profile: str, configured=None) -> list[dict]:
    models = _catalog_for(profile)
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
                        for item in _models_for_profile(profile, configured))}
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
               for entry in pool.entries())


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
                models = _usable_models(account.get("models"))
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
                profile = entry.get("profile")
                if not profile or not _in_region(profile, region):
                    continue
                zero = pool._zero_balance(entry, profile)
                if not zero and not pool._has_credit(entry, profile):
                    continue
                account = accounts.get(entry.get("account_key")) or {}
                if account.get("profile") != profile:
                    continue
                for item in _usable_models(account.get("models")):
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
    body.setdefault("model", "auto")
    guard_model(body["model"], region=region)
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages or any(not isinstance(message, dict) for message in messages):
        raise HTTPException(status_code=400, detail={"error": {
            "message": "messages must be a non-empty array of objects", "type": "invalid_request_error"}})
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


def _guard_request_size(body: dict) -> None:
    """限制处理后发往上游的 JSON 字节数，不截断文本或工具参数。"""
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


def guard_model(name: str, *, region=None) -> None:
    """表外模型本地拒绝；自动路由只考虑各账号明确支持的模型。"""
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(status_code=400, detail={"error": {
            "message": "model must be a non-empty string", "type": "invalid_request_error", "param": "model"}})
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
            for item in current_model_details()]
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
    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {"message": "messages is required", "type": "invalid_request_error"}})

    # 构造后端 body：只透传已知的合法字段
    client_wants_stream = bool(payload.get("stream"))
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
    body, cred, headers, url = _route_chat(payload, body, rid)
    _log_json(f"[{rid}] REQUEST BODY (发往后端，预览)", body)
    t0 = time.time()

    if client_wants_stream:
        return StreamingResponse(
            _stream_upstream(url, headers, body, model_name, t0, rid, cred=cred),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式：后端只支持流式，这里把后端 SSE 聚合成单个 chat.completion 响应
    try:
        collected = await _fetch_checked_chat(url, headers, body, model_name, rid, cred)
    except (httpx.HTTPError, UpstreamResponseError) as error:
        status, raw = _upstream_failure(error, model_name, t0, rid)
        raise HTTPException(status_code=status, detail=_safe_err_raw(raw, status)) from None
    _log_finish(model_name, t0, collected, rid)
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
    tcs = msg.get("tool_calls") or []
    usage = result.get("usage") or {}
    tag = ""
    if finish == "content-filter":
        tag = " ⚠️内容审核拦截"
    tc_names = [t.get("function", {}).get("name") for t in tcs]
    _log(f"{prefix}◀ RESPONSE {model_name} | {elapsed:.1f}s | finish={finish}{tag}"
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


async def _collect_stream(response: httpx.Response) -> dict:
    """使用公共聚合器保留正文、思考和工具调用，并验证流完整性。"""
    accumulator = ChatSSEAccumulator()
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


def _tool_calls_healthy(tool_calls) -> bool:
    """校验聚合后的 tool_calls：name 非空且 arguments 为合法 JSON。"""
    if not tool_calls:
        return True
    for tc in tool_calls:
        if not isinstance(tc.get("id"), str) or not tc["id"].strip():
            return False
        fn = tc.get("function") or {}
        if not (fn.get("name") or "").strip() or not (fn.get("arguments") or "").strip():
            return False
        try:
            json.loads(fn.get("arguments") or "")
        except Exception:
            return False
    return True


def _merge_chat_sse_text(text: str) -> dict:
    """文本路径与异步流路径使用同一聚合器。"""
    accumulator = ChatSSEAccumulator()
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

    def _line(delta: dict, fr=None) -> str:
        payload = {"choices": [{"index": 0, "delta": delta, "finish_reason": fr}]}
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
        lines.append("data: " + json.dumps({"choices": [], "usage": m["usage"]}, ensure_ascii=False))
    lines.append("data: [DONE]")
    return lines


def _network_error_text(error: Exception) -> str:
    return sanitize_log_text(f"{type(error).__name__}: {str(error).strip() or 'upstream transport failed'}", 512)


def _backend_stream(url, headers, body, *, timeout=300, rid="", model_name="?"):
    return open_backend_stream(
        url, headers, body, read_timeout=timeout,
        on_retry=lambda error: _log(
            f"[{rid}] 建连失败，重试 1/1 | {model_name} | {_network_error_text(error)}"),
    )


def _safe_err_raw(raw: bytes, status: int) -> dict:
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {"error": {"message": raw.decode("utf-8", "replace")[:500], "type": "upstream_error", "code": status}}


def _check_upstream_status(status, raw, cred, model):
    if status != 200:
        _note_cred_status(cred, status, model=model, raw=raw)
        raise UpstreamResponseError(status, raw)


def _upstream_failure(error, model_name, t0, rid):
    """统一失败日志与错误体，协议包装由各端点负责。"""
    if isinstance(error, UpstreamResponseError):
        status, raw = error.status, error.raw
        category = f"HTTP {status}"
    else:
        status, raw = 502, _network_error_text(error).encode("utf-8")
        category = "网络错误"
    elapsed = time.time() - t0 if t0 else 0
    _log(f"[{rid}] ✗ {category} | {model_name} | {elapsed:.1f}s | {sanitize_log_text(raw.decode('utf-8', 'replace'), 512)}")
    _log_text_body(f"[{rid}] ERROR BODY", raw.decode("utf-8", "replace"))
    return status, raw


async def _fetch_checked_chat(url, headers, body, model_name, rid, cred=None, *, filter_retry=False):
    """统一聚合与工具校验；仅工具损坏可重新生成，网络错误不整单重放。"""
    attempts = _TOOL_CALL_MAX_RETRY + 1 if body.get("tools") else 1
    for attempt in range(attempts):
        if filter_retry:
            status, raw, _ = await _post_backend_with_filter_retry(url, headers, body, rid, model_name)
            _check_upstream_status(status, raw, cred, body.get("model"))
            result = _chat_completion(_merge_chat_sse_text(raw.decode("utf-8", "replace")))
        else:
            async with _backend_stream(url, headers, body, rid=rid, model_name=model_name) as response:
                if response.status_code != 200:
                    _check_upstream_status(response.status_code, await response.aread(), cred, body.get("model"))
                result = await _collect_stream(response)
        calls = result["choices"][0]["message"].get("tool_calls")
        if _tool_calls_healthy(calls) and _tool_choice_satisfied(calls, body):
            return result
        if attempt + 1 < attempts:
            _log(f"[{rid}] tool_calls 损坏，重试 {attempt + 1}/{attempts - 1} | {model_name}")
    raise UpstreamResponseError(502, b"Invalid upstream tool_calls after retries")


async def _chat_sse_lines(url, headers, body, model_name, t0, rid, cred=None, *, aggregate=False, filter_retry=False):
    """提供公共 Chat SSE 行流；日志仅缓存预览，透传分支不缓存完整正文。"""
    if aggregate:
        result = await _fetch_checked_chat(url, headers, body, model_name, rid, cred, filter_retry=filter_retry)
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
            _check_upstream_status(response.status_code, await response.aread(), cred, body.get("model"))
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
    _log(f"[{rid}] ◀ RESPONSE {model_name} | {time.time() - t0:.1f}s | stream finish={merged['finish_reason']}"
         + f" | tokens={(merged['usage'] or {}).get('total_tokens', '?')}")
    _log_text_body(f"[{rid}] RESPONSE SSE PREVIEW", preview.decode("utf-8", "replace"))


async def _stream_upstream(url: str, headers: dict, body: dict,
                           model_name: str = "?", t0: float = 0.0, rid: str = "", cred=None):
    try:
        async for line in _chat_sse_lines(url, headers, body, model_name, t0, rid, cred, aggregate=bool(body.get("tools"))):
            yield (line + "\n").encode("utf-8")
    except (httpx.HTTPError, UpstreamResponseError) as error:
        status, raw = _upstream_failure(error, model_name, t0, rid)
        yield _err_event(raw, status)




def _err_event(msg: bytes, status: int) -> bytes:
    chunk = {"error": {"message": sanitize_log_text(msg.decode("utf-8", "replace"), 512),
                       "type": "upstream_error", "code": status}}
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")


def _looks_like_content_filter_text(text: str) -> bool:
    text = (text or "").lower()
    return (
        "content-filter" in text
        or "content_filter" in text
        or "敏感内容" in text
        or "内容审核" in text
        or "无法响应您的请求" in text
    )


def _chat_body_desensitize(body: dict, *, force_compact: bool = False) -> dict:
    if not CONFIG.get("desensitize"):
        return body
    return desensitize_body(
        body,
        roles=("system", "developer"),
        desensitize_harness_user=True,
        desensitize_tools=True,
        compact_harness=(force_compact or not CONFIG.get("no_compact")),
        strip_tool_metadata=True,
    )


async def _post_backend_once(url: str, headers: dict, body: dict, *, rid="") -> tuple[int, bytes]:
    async with _backend_stream(url, headers, body, timeout=120, rid=rid, model_name=body.get("model", "?")) as r:
        if r.status_code != 200:
            return r.status_code, await r.aread()
        lines = []
        async for line in r.aiter_lines():
            lines.append(line)
            if line.strip().startswith("data:") and line.strip()[5:].strip() == "[DONE]":
                break
        return r.status_code, ("\n".join(lines) + "\n").encode("utf-8")


async def _post_backend_with_filter_retry(url: str, headers: dict, body: dict,
                                          rid: str = "", model_name: str = "?") -> tuple[int, bytes, dict]:
    prefix = f"[{rid}] " if rid else ""
    status, raw = await _post_backend_once(url, headers, body, rid=rid)
    text = raw.decode("utf-8", "replace")
    if status == 200 and _looks_like_content_filter_text(text) and CONFIG.get("desensitize") and CONFIG.get("no_compact"):
        retry_body = _chat_body_desensitize(body, force_compact=True)
        _log(f"{prefix}↻ RESPONSES {model_name} | content filter detected, retry with compact harness")
        try:
            _guard_request_size(retry_body)
        except HTTPException:
            return status, raw, body
        _log_json(f"{prefix}RESPONSES RETRY CHAT BODY (预览)", retry_body)
        retry_status, retry_raw = await _post_backend_once(url, headers, retry_body, rid=rid)
        retry_text = retry_raw.decode("utf-8", "replace")
        if retry_status == 200 and not _looks_like_content_filter_text(retry_text):
            return retry_status, retry_raw, retry_body
    return status, raw, body


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
    # 转换请求：Responses → Chat
    try:
        chat_body = responses_request_to_chat(payload)
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"request conversion error: {e}", "type": "invalid_request_error"}})

    chat_body, projection_stats = project_responses_chat_body(chat_body)
    chat_body = _prepare_chat_body(chat_body)

    client_wants_stream = payload.get("stream", True)  # Codex CLI 默认 stream
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
    chat_body, cred, headers, url = _route_chat(payload, chat_body, rid)
    _log_json(f"[{rid}] RESPONSES → CHAT BODY (预览)", chat_body)
    t0 = time.time()

    if client_wants_stream:
        return StreamingResponse(
            _stream_responses(url, headers, chat_body, model_name, t0, rid, cred=cred),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return await _nonstream_adapted(url, headers, chat_body, model_name, t0, rid, cred)


async def _nonstream_adapted(url, headers, body, model_name, t0, rid, cred, *, anthropic=False):
    converter = AnthropicStreamConverter(model=model_name) if anthropic else ResponsesStreamConverter(model=model_name)
    try:
        collected = await _fetch_checked_chat(url, headers, body, model_name, rid, cred, filter_retry=not anthropic)
        for line in _chat_result_to_sse_lines(_completion_to_merged(collected)):
            converter.feed_line(line)
        converter.finish()
    except (httpx.HTTPError, UpstreamResponseError) as error:
        status, raw = _upstream_failure(error, model_name, t0, rid)
        raise HTTPException(status_code=status, detail=_safe_err_raw(raw, status)) from None
    result = converter.get_nonstream_response()
    _log_finish(model_name, t0, collected, rid)
    return JSONResponse(content=result)


async def _stream_adapted(url, headers, body, model_name, t0, rid, cred=None, *, anthropic=False):
    """协议适配只处理事件映射，连接、聚合与错误边界共用。"""
    converter = AnthropicStreamConverter(model=model_name) if anthropic else ResponsesStreamConverter(model=model_name)
    try:
        async for line in _chat_sse_lines(
                url, headers, body, model_name, t0, rid, cred,
                aggregate=not anthropic or bool(body.get("tools")), filter_retry=not anthropic):
            events = converter.feed_line(line)
            if events:
                yield events.encode("utf-8")
        events = converter.finish()
        if events:
            yield events.encode("utf-8")
    except (httpx.HTTPError, UpstreamResponseError) as error:
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
    chat_body, cred, headers, url = _route_chat(payload, chat_body, rid)
    _log_json(f"[{rid}] ANTHROPIC → CHAT BODY (预览)", chat_body)
    t0 = time.time()

    if not payload.get("stream", True):
        return await _nonstream_adapted(url, headers, chat_body, model_name, t0, rid, cred, anthropic=True)

    return StreamingResponse(
        _stream_anthropic(url, headers, chat_body, model_name, t0, rid, cred=cred),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _stream_anthropic(url: str, headers: dict, body: dict,
                            model_name: str = "?", t0: float = 0.0, rid: str = "", cred=None):
    async for chunk in _stream_adapted(url, headers, body, model_name, t0, rid, cred, anthropic=True):
        yield chunk


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request,
                       authorization: Optional[str] = Header(default=None),
                       x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Anthropic token 计数端点（stub）。

    Claude Code 可能在发送消息前调用此端点。
    返回一个简单估算值，不做实际 token 计数。
    """
    _check_auth(authorization, x_api_key)
    return {"input_tokens": 0}


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
                    help="login 使用的站点：cn 国内站（默认），intl 国际站")
    ap.add_argument("--no-browser", action="store_true",
                    help="login 仅显示授权链接，不自动打开浏览器（服务器/容器环境）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--api-key", default=os.environ.get("CODEBUDDY2API_KEY", ""),
                    help="可选：要求客户端携带的 API key（默认不校验）")
    ap.add_argument("--log", default=None, metavar="PATH",
                    help="开启日志并写到该文件（如 --log converter.log 或 --log /tmp/cb.log）。"
                         "不传则不记日志。")
    ap.add_argument("--desensitize", action="store_true",
                    help="启用脱敏：对 system 消息里的合规模板敏感词（DoS/exploit/credential 等）"
                         "插入零宽空格，缓解被后端内容审核误拦。默认关闭。")
    ap.add_argument("--no-compact", action="store_true",
                    help="配合 --desensitize 使用：跳过 system/harness 压缩，仅做零宽脱敏。"
                         "保留原始 system prompt 完整内容（如 Claude Code 的行为指令），"
                         "但审核误拦风险略高于默认压缩模式。")
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
    ap.add_argument("--log-body-limit", type=_nonnegative_int, metavar="BYTES",
                    default=os.environ.get("CODEBUDDY2API_LOG_BODY_LIMIT", "65536"),
                    help="每条正文日志的预览字节上限，默认 64 KiB；0 只记录摘要")
    ap.add_argument("--auto-trial", type=_boolean_arg, nargs="?", const=True,
                    default=os.environ.get("CODEBUDDY2API_AUTO_TRIAL", "false"),
                    help="自动领取国际 WorkBuddy 一次性体验积分，默认关闭")
    args = ap.parse_args()
    if args.image_policy not in ("truncate", "error"):
        ap.error("CODEBUDDY2API_IMAGE_POLICY 必须为 truncate 或 error")
    if args.command == "login":
        return login(site=args.site, open_browser=not args.no_browser)

    for key in ("max_images", "image_policy", "max_request_bytes", "log_body_limit", "auto_trial"):
        CONFIG[key] = getattr(args, key)
    CONFIG["api_key"] = args.api_key
    CONFIG["trial_ledger"] = (trial_rewards.TrialLedger(managed_auth_dir() / "trial-ledger.json")
                              if args.auto_trial else None)
    CONFIG["desensitize"] = args.desensitize
    CONFIG["no_compact"] = args.no_compact
    CONFIG["credit_price_cny"] = args.credit_price_cny or None
    CONFIG["usd_rate"] = args.usd_rate or None
    CONFIG["credit_price_usd"] = args.credit_price_usd or None
    CONFIG["model_guard"] = not args.no_model_guard
    # --log 直接指定文件路径即开启；不传则不记
    CONFIG["log_path"] = args.log if args.log else os.environ.get("CODEBUDDY2API_LOG")
    files = [Path(p) for p in args.auth_file]
    if not files:
        seed_credentials()  # 自管模式：启动时把桌面端缺失凭据复制进 auth/
    CONFIG["cred_pool"] = CredentialPool(files, scan=not files)
    CONFIG["cred"] = CONFIG["cred_pool"].first()
    CONFIG["account_catalogs"] = {}  # 在任何维护线程/预检启动前关闭静态兜底。
    if credits_mod is not None:
        ledger = credits_mod.CreditLedger(managed_auth_dir() / "credits-ledger.json")
        CONFIG["ledger"] = ledger
        CONFIG["model_cache"] = credits_mod.ModelCatalogCache(
            managed_auth_dir() / "model-catalog.json", ttl=args.model_catalog_ttl)
        CONFIG["cred_pool"].set_ledger(ledger)  # 先验证持久余额所属身份，再发布目录（包括空表）。
    _publish_model_cache()
    threading.Thread(target=_refresher_loop, args=(CONFIG["cred_pool"],),
                     daemon=True, name="cred-refresher").start()
    if credits_mod is not None:
        threading.Thread(target=_housekeeper_loop, args=(CONFIG["cred_pool"], ledger),
                         daemon=True, name="cred-housekeeper").start()

    if not args.skip_check:
        preflight()

    sys.stderr.write(f"\n✅ 监听 http://{args.host}:{args.port}（直连后端，原生 function calling）\n")
    sys.stderr.write("   GET  /v1/models\n")
    sys.stderr.write("   POST /v1/chat/completions   (原生 tools/tool_calls，支持流式)\n")
    sys.stderr.write("   POST /v1/responses          (Responses API，Codex CLI 兼容)\n")
    sys.stderr.write("   POST /v1/messages           (Anthropic API，Claude Code / CC Switch 兼容)\n")
    sys.stderr.write("   GET  /health\n")
    sys.stderr.write("   GET/POST/DELETE /admin/credentials  (凭证池管理)\n")
    sys.stderr.write("   添加账号：python3 converter.py login（自动等待扫码并保存）\n")
    if credits_mod is not None:
        sys.stderr.write("   GET  /admin/credits           (积分/签到状态)\n")
        sys.stderr.write("   POST /admin/checkin           (手动触发签到+积分刷新)\n")
        sys.stderr.write("   每日签到 + 快过期积分优先调度已启用\n")
    if args.api_key:
        sys.stderr.write("   鉴权已启用（API key 已设置）\n")
    sys.stderr.write(f"   图片限制  : {CONFIG['max_images']} 张/请求，策略 {CONFIG['image_policy']}\n")
    if CONFIG["log_path"]:
        sys.stderr.write(f"   日志      : {CONFIG['log_path']}\n")
    if args.desensitize:
        mode = "零宽脱敏 + 保留全文" if args.no_compact else "零宽脱敏 + 压缩摘要"
        sys.stderr.write(f"   脱敏      : 已启用（{mode}）\n")
    sys.stderr.write("按 Ctrl+C 退出。\n\n")

    # 启动时写一条标记
    _log(f"==== converter 启动 ====")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    sys.exit(main())
