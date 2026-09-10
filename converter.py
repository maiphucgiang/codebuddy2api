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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

try:
    from desensitize import desensitize_body
except ImportError:  # 模块缺失时降级为不脱敏
    def desensitize_body(body, roles=("system",), desensitize_harness_user=False,
                         desensitize_tools=False, compact_harness=False,
                         strip_tool_metadata=False):
        return body

from responses_adapter import (
    responses_request_to_chat,
    ResponsesStreamConverter,
)
from responses_projection import project_responses_chat_body
from anthropic_adapter import (
    anthropic_request_to_chat,
    AnthropicStreamConverter,
)

import auth_oauth
from credential_io import CredentialFileError, read_import_file, atomic_write_credential
try:
    import credits as credits_mod
except ImportError:  # 模块缺失时签到/积分/快过期优先调度不可用
    credits_mod = None

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

APP_VERSION = Path(__file__).with_name("VERSION").read_text(encoding="utf-8").strip()
BACKEND = "https://copilot.tencent.com"
DEFAULT_DOMAIN = "www.codebuddy.cn"
CBC_VERSION = "2.148.0"      # 掩盖用的官方 cbc 客户端版本（随本机 @tencent-ai/codebuddy-code 更新）
USER_AGENT = f"CLI/{CBC_VERSION} CodeBuddy/{CBC_VERSION}"

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
    have_uids = {u for u in (_cred_uid(f) for f in dst_dir.glob("*.info")) if u}
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
            if uid and uid in have_uids:
                _log(f"[cred] 种子跳过（同账号已在自管目录）: {f.name}")
                continue
            dst = dst_dir / f.name
            if not dst.exists():
                try:
                    shutil.copyfile(f, dst)
                    os.chmod(dst, 0o600)
                    if uid:
                        have_uids.add(uid)
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
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._mtime: float = 0.0

    def _read_raw(self) -> dict:
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _load_if_stale(self):
        """若文件 mtime 变了（外部刷新过），重新加载缓存。"""
        try:
            mt = self.path.stat().st_mtime
        except OSError:
            return
        if self._cached is None or mt != self._mtime:
            self._cached = self._read_raw()
            self._mtime = mt

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

    def _refresh(self):
        """调后端刷新 token，写回 auth 文件与缓存。"""
        s = self._session()
        auth = s.get("auth") or {}
        headers = self._build_headers_from(auth, s.get("account") or {})
        headers["X-Refresh-Token"] = auth.get("refreshToken", "")
        headers["X-Auth-Refresh-Source"] = "plugin"
        url = f"{BACKEND}/v2/plugin/auth/token/refresh"
        try:
            with httpx.Client(timeout=15) as c:
                r = c.post(url, headers=headers, json={})
            data = r.json()
        except Exception as e:
            raise RuntimeError(f"刷新 token 网络失败：{e}")
        if data.get("code") != 0 or not data.get("data"):
            raise RuntimeError(f"刷新 token 失败：{data.get('msg', data)}")
        new_auth = data["data"]
        # 继承部分字段
        new_auth["domain"] = new_auth.get("domain") or auth.get("domain")
        new_auth["lastRefreshTime"] = int(time.time() * 1000)
        # 计算 expiresAt（若后端没直接给）
        if not new_auth.get("expiresAt") and new_auth.get("expiresIn"):
            new_auth["expiresAt"] = int(time.time() * 1000) + new_auth["expiresIn"] * 1000
        if not new_auth.get("refreshExpiresAt") and new_auth.get("refreshExpiresIn"):
            new_auth["refreshExpiresAt"] = int(time.time() * 1000) + new_auth["refreshExpiresIn"] * 1000
        s["auth"] = new_auth
        # 原子写回
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        self._cached = s
        self._mtime = self.path.stat().st_mtime

    def _build_headers_from(self, auth: dict, account: dict) -> dict:
        domain = auth.get("domain") or DEFAULT_DOMAIN
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {auth.get('accessToken','')}",
            "X-User-Id": account.get("uid", ""),
            "X-Enterprise-Id": account.get("enterpriseId", ""),
            "X-Tenant-Id": account.get("enterpriseId", ""),
            "X-Domain": domain,
            "User-Agent": USER_AGENT,
        }
        h.update(_STATIC_CLIENT_HEADERS)  # 掩盖：与官方 cbc 客户端保持一致的静态头
        return h

    def get_headers(self) -> dict:
        """返回带最新 token 的后端请求 header；必要时先刷新。"""
        with self._lock:
            if self._is_expired():
                self._refresh()
            s = self._session()
            return self._build_headers_from(s.get("auth") or {}, s.get("account") or {})

    def summary(self) -> dict:
        s = self._session()
        auth = s.get("auth") or {}
        acct = s.get("account") or {}
        exp = auth.get("expiresAt", 0)
        return {
            "uid": acct.get("uid"),
            "nickname": acct.get("nickname"),
            "enterpriseName": acct.get("enterpriseName"),
            "token_expires_at": exp,
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


# 官方 cbc 客户端的静态请求头（实测捕获自 codebuddy-code 2.141.0，用于风控掩盖）
_STATIC_CLIENT_HEADERS = {
    "x-requested-with": "XMLHttpRequest",
    "x-stainless-arch": "x64",
    "x-stainless-lang": "js",
    "x-stainless-os": "Linux",
    "x-stainless-package-version": "6.25.0",
    "x-stainless-retry-count": "0",
    "x-stainless-runtime": "node",
    "x-stainless-runtime-version": "v24.20.0",
    "X-Agent-Intent": "craft",
    "X-Agent-Purpose": "conversation",
    "X-Agent-Type": "main",
    "X-IDE-Type": "CLI",
    "X-IDE-Name": "CLI",
    "X-IDE-Version": CBC_VERSION,
    "X-Private-Data": "false",
    "X-CodeBuddy-Request": "1",
    "X-Product": "SaaS",
}


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
        self._lock = threading.Lock()
        self._entries: list[dict] = []   # {id, cm, fail_until}
        self._sticky: "OrderedDict[str, tuple[str, float]]" = OrderedDict()
        self._model_fail: dict[tuple[str, str], float] = {}  # (cred_id, model) -> 冷却截止 epoch（429 模型级冷却）
        self._rr = 0
        self._ledger = None              # CreditLedger：pick 时按积分最早过期时间优先调度
        self._scan = scan                # True 时 pick 前自动扫描目录增删凭证
        self.reload(paths or [])
        if self._scan:
            self._rescan()             # 启动即发现一轮，/health 不等首个请求

    def reload(self, paths: list[Path]):
        """加入新凭据文件；同名文件重读磁盘（覆盖导入场景），同账号(uid)不同文件的忽略并告警。"""
        with self._lock:
            by_id = {e["id"]: e for e in self._entries}
            have_uids = {e.get("uid"): e["id"] for e in self._entries if e.get("uid")}
            for p in paths:
                pid = str(Path(p).resolve())
                if not os.path.exists(pid):
                    continue
                if pid in by_id:
                    by_id[pid]["cm"] = CredentialManager(Path(pid))  # 重读磁盘，防覆盖导入后内存态陈旧
                    by_id[pid]["fail_until"] = 0.0
                    continue
                cm = CredentialManager(Path(pid))
                try:
                    uid = cm.summary().get("uid")
                except Exception:
                    uid = None
                if uid and uid in have_uids:
                    _log(f"[cred] 忽略重复账号凭据: {Path(pid).name}（uid 与 {Path(have_uids[uid]).name} 相同）")
                    continue
                self._entries.append({"id": pid, "cm": cm, "fail_until": 0.0, "uid": uid})
                if uid:
                    have_uids[uid] = pid
    def prune(self):
        """移除已不存在文件的凭据，并清理其黏绑。"""
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if os.path.exists(e["id"])]
            if len(self._entries) != before:
                ids = {e["id"] for e in self._entries}
                self._sticky = OrderedDict((k, v) for k, v in self._sticky.items() if v[0] in ids)
                self._model_fail = {k: v for k, v in self._model_fail.items() if k[0] in ids}


    def find_by_uid(self, uid: str) -> Optional[str]:
        """按账号 uid 查池内凭据 id（用于导入冲突检测）。"""
        with self._lock:
            for e in self._entries:
                if e.get("uid") == uid:
                    return e["id"]
        return None

    def set_ledger(self, ledger):
        """挂接 CreditLedger 后，pick 按积分最早过期时间优先选凭证。"""
        self._ledger = ledger

    def entries(self) -> list[dict]:
        """池内凭证条目快照（供签到/积分调度遍历）。"""
        with self._lock:
            return [dict(e) for e in self._entries]

    def _expiry_rank(self, e: dict) -> tuple:
        """快过期优先排序键：(无数据排后, 最早过期时间升序)。"""
        exp = self._ledger.soonest_expiry_of(e["id"]) if self._ledger else None
        return (exp is None, exp or 0.0)
    def _rescan(self):
        if not self._scan:
            return
        self.reload(find_auth_files())
        self.prune()

    def _healthy(self, e: dict) -> bool:
        return time.time() >= e["fail_until"]

    def _model_healthy(self, e: dict, model: str | None) -> bool:
        """该凭证对指定模型未处于 429 冷却期；model 为空时不做模型级检查。"""
        if not model:
            return True
        return time.time() >= self._model_fail.get((e["id"], model), 0.0)

    def _evict_sticky(self):
        now = time.time()
        while self._sticky:
            k, (_, ts) = next(iter(self._sticky.items()))
            if now - ts > STICKY_TTL or len(self._sticky) > STICKY_MAX:
                self._sticky.pop(k)
            else:
                break

    def pick(self, skey: str | None, model: str | None = None) -> CredentialManager | None:
        """按黏绑选凭证；未绑定/已失效则轮询取健康凭证并绑定。

        model 非空时跳过该模型 429 冷却中的凭证（黏性会话自动换绑）；
        全部凭证对该模型冷却时返回 None，由上层快速失败，不再打上游。
        """
        self._rescan()  # 锁外扫描，reload/prune 各自取锁，避免死锁
        with self._lock:
            self._evict_sticky()
            if skey and skey in self._sticky:
                cid, _ = self._sticky[skey]
                e = next((x for x in self._entries if x["id"] == cid), None)
                if e and self._healthy(e) and self._model_healthy(e, model):
                    self._sticky[skey] = (cid, time.time())
                    self._sticky.move_to_end(skey)
                    return e["cm"]
                self._sticky.pop(skey, None)
            if not self._entries:
                return None
            if model:
                # 模型级冷却严格生效，不做或全量回退
                healthy = [e for e in self._entries
                           if self._healthy(e) and self._model_healthy(e, model)]
            else:
                healthy = [e for e in self._entries if self._healthy(e)] or self._entries
            if not healthy:
                return None
            healthy.sort(key=self._expiry_rank)  # 快过期积分优先；无数据排最后（稳定排序保原顺序）
            top = [e for e in healthy if self._expiry_rank(e) == self._expiry_rank(healthy[0])]
            e = top[self._rr % len(top)]  # 同优先级内轮询，分散单凭证压力
            self._rr += 1
            if skey:
                self._sticky[skey] = (e["id"], time.time())
            return e["cm"]

    def headers_for(self, skey: str | None, model: str | None = None):
        """返回 (CredentialManager, headers)；get_headers 失败自动熔断换下一个。"""
        for _ in range(max(1, len(self._entries))):
            cm = self.pick(skey, model)
            if cm is None:
                return None
            try:
                return cm, cm.get_headers()
            except Exception as e:
                self.cooldown(cm, reason=str(e))
        return None

    def cooldown(self, cm: CredentialManager, reason: str = ""):
        with self._lock:
            for e in self._entries:
                if e["cm"] is cm:
                    e["fail_until"] = time.time() + CRED_COOLDOWN
        _log(f"[cred] 凭证熔断 {CRED_COOLDOWN}s: {Path(cm.path).name} {reason}")

    def note_status(self, cm: CredentialManager | None, status: int,
                    model: str | None = None, raw: bytes = b""):
        """401/403 熔断整个凭证；429 只冷却 (凭证,模型) 至配额重置时间，其他模型/凭证不受影响。"""
        if cm is None:
            return
        if status in (401, 403):
            self.cooldown(cm, reason=f"backend HTTP {status}")
            return
        if status != 429 or not model:
            return
        now = time.time()
        until = _parse_reset_time(raw) or now + MODEL_COOLDOWN
        until = min(until, now + MODEL_COOLDOWN_MAX)
        with self._lock:
            self._model_fail = {k: v for k, v in self._model_fail.items() if v > now}
            for e in self._entries:
                if e["cm"] is cm:
                    self._model_fail[(e["id"], model)] = until
        _log(f"[cred] 模型冷却 {model} @ {Path(cm.path).name} 至 "
             f"{time.strftime('%m-%d %H:%M:%S', time.localtime(until))} (HTTP 429)")

    def model_cooldown_until(self, model: str | None) -> float | None:
        """该模型在所有健康凭证上都在冷却时返回最早恢复时间；否则 None。"""
        if not model:
            return None
        with self._lock:
            now = time.time()
            pool = [e for e in self._entries if self._healthy(e)]
            if not pool:
                return None
            untils = [self._model_fail.get((e["id"], model), 0.0) for e in pool]
            if any(now >= u for u in untils):
                return None
            return min(untils)

    def refresh_due(self, margin_s: int = CRED_REFRESH_MARGIN, keepalive_s: int = CRED_KEEPALIVE_S):
        """刷新即将过期的凭证并回写文件；距上次刷新超 keepalive_s 的做每日保活刷新。"""
        with self._lock:
            entries = list(self._entries)
        now = time.time()
        for e in entries:
            try:
                s = e["cm"].summary()
            except Exception as ex:
                self.cooldown(e["cm"], reason=str(ex))
                continue
            exp = (s.get("token_expires_at") or 0) / 1000
            last = (s.get("last_refresh_time") or 0) / 1000
            expiry_due = bool(s.get("token_expired") or (exp and exp - now < margin_s))
            keepalive_due = (not expiry_due and keepalive_s > 0
                             and now >= e.get("keepalive_after", 0.0)
                             and (last <= 0 or now - last >= keepalive_s))
            if not (expiry_due or keepalive_due):
                continue
            try:
                e["cm"].get_headers()
                e["keepalive_after"] = 0.0
                _log(f"[cred] {'每日保活刷新' if keepalive_due else '已主动刷新'}并回写: {Path(e['id']).name}")
            except Exception as ex:
                if keepalive_due:  # 保活失败按独立间隔重试，不打乱临期重试节奏
                    e["keepalive_after"] = now + CRED_KEEPALIVE_RETRY_S
                self.cooldown(e["cm"], reason=str(ex))
    def remove_file(self, name: str) -> bool:
        """按文件名移除凭据（含删除文件）。"""
        with self._lock:
            e = next((x for x in self._entries if os.path.basename(x["id"]) == name), None)
        if e is None:
            return False
        try:
            os.unlink(e["id"])
        except OSError:
            pass
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


def _housekeep_once(pool: CredentialPool, ledger) -> None:
    """对池内每个凭证：今日签到（幂等）→ 刷新积分缓存（供快过期优先调度）。"""
    if credits_mod is None:
        return
    day = time.strftime("%Y-%m-%d")
    catalog_refs: dict[str, str] = {}  # 站点组 → 该组任一有额度凭证的 token（用于拉模型表）
    for e in pool.entries():
        cm, cid = e["cm"], e["id"]
        name = Path(cid).name
        if not ledger.checkin_done(cid, day):
            try:
                h = cm.get_headers()  # 过期自动刷新
                r = credits_mod.daily_checkin(_bearer_token(h), uid=h.get("X-User-Id", ""), domain=h.get("X-Domain", ""))
                ledger.mark_checkin(cid, day, r["ok"], r.get("code"), r.get("message", ""))
                _log(f"[checkin] {name}: ok={r['ok']} already={r.get('already')} code={r.get('code')} {r.get('message', '')}")
            except Exception as ex:
                ledger.note_error(cid, f"checkin: {ex}")
                _log(f"[checkin] {name} 异常: {ex}")
        try:
            h = cm.get_headers()
            r = credits_mod.fetch_credits(_bearer_token(h), uid=h.get("X-User-Id", ""), domain=h.get("X-Domain", ""))
            ledger.update_credits(cid, r)
            grp = "international" if r.get("intl") else "domestic"
            if float(r.get("credits") or 0) > 0:  # 有额度才作为该站模型表的拉取凭据
                catalog_refs.setdefault(grp, _bearer_token(h))
            exp = r.get("soonest_expiry")
            exp_s = time.strftime("%m-%d %H:%M", time.localtime(exp)) if exp else "-"
            _log(f"[credits] {name}: 余额 {r['credits']}，{len(r['segments'])} 段，最早过期 {exp_s}")
        except Exception as ex:
            ledger.note_error(cid, f"credits: {ex}")
            _log(f"[credits] {name} 查询失败: {ex}")
    # 用量明细同步：官方真实 credit 扣减（限最近 30 天），按站点分组供折算与 daily_costs 使用
    by_day: dict = {}
    group_usage: dict = {}
    used_detail = 0.0
    req_count = 0
    detail_ok = False
    for entry in pool.entries():
        try:
            h = entry["cm"].get_headers()
            tok = _bearer_token(h)
            grp = ("international"
                   if credits_mod.is_international_host(credits_mod.hosts_for_token(tok)[0])
                   else "domestic")
            u = credits_mod.fetch_request_usage(tok, uid=h.get("X-User-Id", ""),
                                                domain=h.get("X-Domain", ""))
            detail_ok = True
            g = group_usage.setdefault(grp, {"by_day": {}, "total_credits": 0.0, "requests": 0})
            for day, models in u["by_day"].items():
                tgt = by_day.setdefault(day, {})
                gtgt = g["by_day"].setdefault(day, {})
                for m, c in models.items():
                    tgt[m] = round(tgt.get(m, 0.0) + c, 6)
                    gtgt[m] = round(gtgt.get(m, 0.0) + c, 6)
            g["total_credits"] += u["total_credits"]
            g["requests"] += u["requests"]
            used_detail += u["total_credits"]
            req_count += u["requests"]
        except Exception as ex:
            _log(f"[usage] {Path(entry['id']).name} 明细拉取失败: {ex}")
    if detail_ok:
        for g in group_usage.values():
            g["total_credits"] = round(g["total_credits"], 2)
        CONFIG["usage_daily"] = {"by_day": by_day, "groups": group_usage,
                                "total_credits": round(used_detail, 2),
                                "requests": req_count, "fetched_at": time.time()}
        _log(f"[usage] 明细已同步: {req_count} 请求 / {used_detail:.2f} credits")

    # 云端模型表：按站点分组，仅「该站有额度凭证」且「TTL 已过期」时才拉，其余走本地缓存
    cache = CONFIG.get("model_cache")
    if cache is not None:
        for grp in ("domestic", "international"):
            token = catalog_refs.get(grp)
            if token is None:
                continue  # 该站无凭证或全部无额度：不拉取，也不对外暴露其模型
            if cache.fresh(grp):
                continue  # TTL 内命中缓存，不打云端
            try:
                models = credits_mod.fetch_model_catalog(token, user_agent=USER_AGENT)
                cache.put(grp, models)
                _log(f"[models] {grp} 模型表已刷新: {len(models)} 个")
            except Exception as ex:
                _log(f"[models] {grp} 模型表同步失败（沿用缓存）: {ex}")
        CONFIG["models_remote"] = cache.models("domestic") or None
        CONFIG["models_intl"] = cache.models("international") or None
        invalidate_model_table()


def _housekeeper_loop(pool: CredentialPool, ledger) -> None:
    """启动 30s 后首跑签到+积分，之后每小时兜底（签到按日幂等）。"""
    time.sleep(CHECKIN_FIRST_DELAY)
    while True:
        try:
            _housekeep_once(pool, ledger)
        except Exception as e:
            _log(f"[housekeeper] 循环异常: {e}")
        time.sleep(HOUSEKEEP_INTERVAL)


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
                "model_guard": True,     # 表外模型本地拦截，不转发上游
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
    """写一行日志到 CONFIG['log_path'] 指定的文件（追加，带时间戳）。未设置则丢弃。"""
    path = CONFIG.get("log_path")
    if not path:
        return
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    try:
        oversized = os.path.getsize(path) >= LOG_MAX_BYTES
    except OSError:
        oversized = False  # 文件不存在等待首次写入
    try:
        with _LOG_LOCK:
            if oversized:
                for i in range(LOG_BACKUPS - 1, 0, -1):  # log.N-1 -> log.N
                    old = f"{path}.{i}"
                    if os.path.exists(old):
                        os.replace(old, f"{path}.{i + 1}")
                os.replace(path, f"{path}.1")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ==== 日志轮转（单文件上限 {LOG_MAX_BYTES // 1024 // 1024}MB，保留 {LOG_BACKUPS} 份） ====\n")
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError:
        pass  # 日志失败不应影响主流程




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


def _cred_for(payload: dict, model: str | None = None):
    """按会话黏绑选凭证并返回 (CredentialManager, headers)；无可用凭证抛 503；模型全凭证冷却时本地抛 429。"""
    skey = session_key(payload)
    pool = CONFIG.get("cred_pool")
    if pool is not None:
        picked = pool.headers_for(skey, model)
        if picked is None:
            until = pool.model_cooldown_until(model)
            if until:
                t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(until))
                raise HTTPException(status_code=429, detail={"error": {
                    "message": f"模型 {model} 额度冷却中（全部凭证），预计 {t} 重置后恢复",
                    "type": "rate_limit_error"}})
            raise HTTPException(status_code=503, detail={"error": {"message": "无可用凭证（未登录、文件缺失或全部熔断）", "type": "auth_error"}})
        cm, headers = picked
    else:
        cm = CONFIG["cred"]
        if cm is None:
            raise HTTPException(status_code=503, detail={"error": {"message": "未找到登录凭据，请先在桌面端登录 CodeBuddy/WorkBuddy", "type": "auth_error"}})
        headers = cm.get_headers()
    headers.update(_dynamic_request_headers(skey))  # 掩盖：每次请求官方同构的追踪/请求 ID
    return cm, headers


def _note_cred_status(cred, status: int, model: str | None = None, raw: bytes = b""):
    """后端 401/403 熔断该凭证；429 按 (凭证,模型) 冷却。黏性会话下次请求自动换绑。"""
    pool = CONFIG.get("cred_pool")
    if pool is not None and cred is not None:
        pool.note_status(cred, status, model=model, raw=raw)

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
    dst = dst_dir / name
    pool = CONFIG.get("cred_pool")
    if pool is not None and src_uid:
        holder = pool.find_by_uid(src_uid)
        if holder and holder != str(dst):
            raise HTTPException(status_code=409, detail={"error": {"message": "该账号已在池中，请使用同文件名更新或先移除旧凭据", "type": "invalid_request_error"}})
    try:
        dst = atomic_write_credential(dst_dir, name, content)
    except CredentialFileError:
        raise HTTPException(status_code=400, detail={"error": {"message": "凭据文件名或保存目标不符合要求", "type": "invalid_request_error"}}) from None
    except OSError:
        raise HTTPException(status_code=500, detail={"error": {"message": "凭据保存失败", "type": "server_error"}}) from None
    if pool is not None:
        pool.reload([dst])
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
    dst_dir = managed_auth_dir()
    dst_dir.mkdir(parents=True, exist_ok=True)
    target = next((f for f in sorted(dst_dir.glob("*.info")) if _cred_uid(f) == uid), None)
    existing = None
    if target is not None:
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            existing = None
    if target is None:
        target = dst_dir / f"{uid}.info"
    cred = auth_oauth.merge_existing_accounts(cred, existing)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(cred, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
    pool = CONFIG.get("cred_pool")
    if pool is not None:
        pool.reload([target])
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
_model_table_cache: tuple = (0.0, [])


def invalidate_model_table() -> None:
    """模型表变更后作废快照缓存。"""
    global _model_table_cache
    _model_table_cache = (0.0, [])


def _has_intl_credits() -> bool:
    """池内是否有「仍有额度」的国际凭证 —— 决定要不要对外暴露国际站模型。"""
    if not (CONFIG.get("models_intl") or []):
        return False
    ledger = CONFIG.get("ledger")
    for e in (ledger.snapshot() if ledger else {}).values():
        c = e.get("credits") or {}
        if c.get("intl") and float(c.get("credits") or 0) > 0:
            return True
    return False


def current_models() -> list[str]:
    """对外模型表：国内云端表 + 国际表（仅有额度国际凭证时）+ 本地 auto 调度别名。

    只接受 supportsToolCall 的对话模型；云端不可用时整体回退 DEFAULT_MODELS。"""
    out: list[str] = [str(m["id"]) for m in (CONFIG.get("models_remote") or [])
                     if m.get("supportsToolCall")]
    if out and _has_intl_credits():
        for m in (CONFIG.get("models_intl") or []):
            mid = str(m.get("id") or "")
            if m.get("supportsToolCall") and mid not in out:
                out.append(mid)
    if not out:
        return list(DEFAULT_MODELS)
    return out + [m for m in DEFAULT_MODELS if m not in out]


def _model_table() -> list[str]:
    """对外模型表快照（短期复用）。"""
    global _model_table_cache
    now = time.time()
    if not _model_table_cache[1] or now - _model_table_cache[0] > _MODEL_TABLE_TTL:
        _model_table_cache = (now, current_models())
    return _model_table_cache[1]


def guard_model(name: str) -> None:
    """表外模型本地拦截：直接 404，不打上游（避免无效请求、额度消耗与风控触发）。"""
    if not CONFIG.get("model_guard") or not name:
        return
    if name in _model_table():
        return
    raise HTTPException(status_code=404, detail={"error": {
        "message": f"The model '{name}' is not supported by this gateway. "
                   "See GET /v1/models for the available list.",
        "type": "invalid_request_error", "param": "model", "code": "model_not_found"}})



@app.get("/v1/models")
def list_models(authorization: Optional[str] = Header(default=None),
                x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_auth(authorization, x_api_key)
    data = [{"id": m, "object": "model", "created": 1700000000, "owned_by": "codebuddy"}
            for m in current_models()]
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

    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {"message": "messages is required", "type": "invalid_request_error"}})

    # 构造后端 body：只透传已知的合法字段
    client_wants_stream = bool(payload.get("stream"))
    body = {k: payload[k] for k in PASSTHROUGH_BODY_KEYS if k in payload}
    body.setdefault("model", "auto")
    guard_model(body["model"])
    # 后端只支持流式：始终以 stream=True 调后端，非流式由转换器聚合
    body["stream"] = True
    if "stream_options" not in body:
        body["stream_options"] = {"include_usage": True}

    # 可选：脱敏。缓解客户端合规模板（如 Codex CLI / ZCode 注入的说明文字）被后端误判为敏感词。
    # 处理 system / developer 消息、Codex 注入的上下文 user 消息，以及 tools 的 description。
    if CONFIG.get("desensitize"):
        body = desensitize_body(body, roles=("system", "developer"),
                                desensitize_harness_user=True,
                                desensitize_tools=True,
                                compact_harness=not CONFIG.get("no_compact"),
                                strip_tool_metadata=True)

    # 日志：请求摘要
    model_name = payload.get("model", "auto")
    tool_names = [t.get("function", {}).get("name") for t in (payload.get("tools") or [])
                  if isinstance(t, dict)]
    last_user = _last_user_text(messages)
    rid = os.urandom(4).hex()
    _log(f"[{rid}] ▶ REQUEST {model_name} | stream={client_wants_stream} | msgs={len(messages)}"
         + (f" | tools={tool_names}" if tool_names else "")
         + (f" | last_user={_truncate(last_user, 60)!r}" if last_user else ""))
    # 完整请求体（发往后端的实际内容；若启用脱敏，这里已是脱敏后）
    _log(f"[{rid}] ── REQUEST BODY (发往后端) ──\n{json.dumps(body, ensure_ascii=False, indent=2)}")

    cred, headers = _cred_for(payload, body.get("model"))
    url = f"{BACKEND}/v2/chat/completions"
    t0 = time.time()

    if client_wants_stream:
        return StreamingResponse(
            _stream_upstream(url, headers, body, model_name, t0, rid, cred=cred),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式：后端只支持流式，这里把后端 SSE 聚合成单个 chat.completion 响应
    try:
        async with httpx.AsyncClient(timeout=300) as c:
            async with c.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code != 200:
                    raw = await r.aread()
                    _note_cred_status(cred, r.status_code, model=body.get("model"), raw=raw)
                    _log(f"[{rid}] ✗ HTTP {r.status_code} | {model_name} | {_truncate(raw.decode('utf-8','replace'),200)}")
                    _log(f"[{rid}] ── ERROR BODY ──\n{raw.decode('utf-8','replace')}")
                    raise HTTPException(status_code=r.status_code, detail=_safe_err_raw(raw, r.status_code))
                collected = await _collect_stream(r)
    except HTTPException:
        raise
    except httpx.HTTPError as e:
        _log(f"[{rid}] ✗ 网络错误 | {model_name} | {e}")
        raise HTTPException(status_code=502, detail={"error": {"message": f"upstream error: {e}", "type": "upstream_error"}})
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
    """记录一次完成的请求：耗时 / finish_reason / usage / 工具调用 / 审核拦截 + 完整响应。"""
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
    # 完整响应体
    _log(f"{prefix}── RESPONSE BODY ──\n{json.dumps(result, ensure_ascii=False, indent=2)}")


async def _collect_stream(response: httpx.Response) -> dict:
    """消费后端的 OpenAI SSE 流，聚合成单个非流式 chat.completion 对象。

    合并所有 chunk 的 delta（content / tool_calls），并取 usage / finish_reason。
    """
    content_parts: list[str] = []
    # tool_calls: index -> {id, name, arguments(分片拼接)}
    tool_calls: dict[int, dict] = {}
    model: str | None = None
    finish_reason: str | None = None
    usage: dict | None = None

    async for line in response.aiter_lines():
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        model = chunk.get("model") or model
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_calls.setdefault(idx, {"id": None, "name": None, "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]

    tcs = None
    if tool_calls:
        tcs = [
            {"id": v["id"], "type": "function",
             "function": {"name": v["name"], "arguments": v["arguments"]}}
            for _, v in sorted(tool_calls.items())
        ]
        finish_reason = finish_reason or "tool_calls"

    message = {"role": "assistant", "content": "".join(content_parts) or None}
    if tcs:
        message["tool_calls"] = tcs
    return {
        "id": "chatcmpl-" + os.urandom(12).hex(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or "unknown",
        "choices": [{"index": 0, "message": message,
                     "finish_reason": finish_reason or "stop"}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


_TOOL_CALL_MAX_RETRY = 3
_STREAM_CONN_RETRY = 1   # 流式请求未产出任何内容即断流时的整请求重试次数（已产出后不重试，避免重复内容/计费）


def _tool_calls_healthy(tool_calls) -> bool:
    """校验聚合后的 tool_calls：name 非空且 arguments 为合法 JSON。"""
    if not tool_calls:
        return True
    for tc in tool_calls:
        fn = tc.get("function") or {}
        if not (fn.get("name") or "").strip() or not (fn.get("arguments") or "").strip():
            return False
        try:
            json.loads(fn.get("arguments") or "")
        except Exception:
            return False
    return True


def _merge_chat_sse_text(text: str) -> dict:
    """把后端 Chat SSE 文本聚合成 {content, tool_calls, finish_reason, usage, model}。"""
    content_parts: list[str] = []
    slots: dict[int, dict] = {}
    finish = usage = model = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        model = chunk.get("model") or model
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                slot = slots.setdefault(tc.get("index", 0), {"id": None, "name": None, "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
    tcs = [{"id": s["id"], "type": "function",
            "function": {"name": s["name"], "arguments": s["arguments"]}}
           for _, s in sorted(slots.items())] if slots else None
    return {"content": "".join(content_parts), "tool_calls": tcs,
            "finish_reason": finish, "usage": usage, "model": model}


def _chat_result_to_sse_lines(m: dict) -> list[str]:
    """把聚合结果伪流式化为标准 OpenAI SSE 文本行（chat 直接转发，anthropic 喂转换器）。"""
    content = m.get("content") or ""
    tcs = m.get("tool_calls") or []
    finish = m.get("finish_reason") or "stop"
    model = m.get("model")

    def _line(delta: dict, fr=None) -> str:
        payload = {"choices": [{"index": 0, "delta": delta, "finish_reason": fr}]}
        if model:
            payload["model"] = model
        return "data: " + json.dumps(payload, ensure_ascii=False)

    lines = [_line({"role": "assistant", "content": ""})]
    for i in range(0, len(content), 48):
        lines.append(_line({"content": content[i:i + 48]}))
    for i, tc in enumerate(tcs):
        lines.append(_line({"tool_calls": [dict(tc, index=i)]}))
    lines.append(_line({}, finish))
    if m.get("usage"):
        lines.append("data: " + json.dumps({"choices": [], "usage": m["usage"]}, ensure_ascii=False))
    lines.append("data: [DONE]")
    return lines


def _safe_err_raw(raw: bytes, status: int) -> dict:
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {"error": {"message": raw.decode("utf-8", "replace")[:500], "type": "upstream_error", "code": status}}


async def _stream_upstream(url: str, headers: dict, body: dict,
                           model_name: str = "?", t0: float = 0.0, rid: str = "", cred=None):
    """转发后端 SSE 给客户端。

    带 tools：聚合 → tool_calls 健康校验 → 损坏重试 → 伪流式转发；
    无 tools：逐 chunk 原样透传，旁路统计 finish_reason / tool_calls / usage 用于日志。
    """
    finish_reason = None
    tool_names: list[str] = []
    usage: dict = {}
    saw_filter = False
    buf = b""
    raw_parts: list[bytes] = []   # 累积完整原始 SSE
    prefix = f"[{rid}] " if rid else ""

    # 后端流式 tool_calls 分片偶发损坏（name 空/arguments 非 JSON），
    # 带 tools 时改为聚合校验重试后伪流式转发，避免下游 agent 拿到坏参数。
    if body.get("tools"):
        collected: dict = {}
        for attempt in range(_TOOL_CALL_MAX_RETRY + 1):
            try:
                async with httpx.AsyncClient(timeout=None) as c:
                    async with c.stream("POST", url, headers=headers, json=body) as r:
                        if r.status_code != 200:
                            err = await r.aread()
                            _note_cred_status(cred, r.status_code, model=body.get("model"), raw=err)
                            _log(f"{prefix}✗ HTTP {r.status_code} | {model_name} | {_truncate(err.decode('utf-8','replace'),200)}")
                            _log(f"{prefix}── ERROR BODY ──\n{err.decode('utf-8','replace')}")
                            yield _err_event(err, r.status_code)
                            return
                        collected = await _collect_stream(r)
            except httpx.HTTPError as e:
                if attempt < _TOOL_CALL_MAX_RETRY:  # 聚合分支未产出过内容，网络错误可安全重试
                    _log(f"{prefix}✗ 网络错误（未产出），重试 {attempt + 1}/{_TOOL_CALL_MAX_RETRY} | {model_name} | {e}")
                    continue
                _log(f"{prefix}✗ 网络错误 | {model_name} | {e}")
                yield _err_event(str(e).encode(), 502)
                return
            choice = (collected.get("choices") or [{}])[0]
            if _tool_calls_healthy((choice.get("message") or {}).get("tool_calls")):
                break
            _log(f"{prefix}▸ 流式 tool_calls 损坏（name 空/参数无效），重试 {attempt + 1}/{_TOOL_CALL_MAX_RETRY}")
        msg = (collected.get("choices") or [{}])[0].get("message") or {}
        m = {"content": msg.get("content") or "",
             "tool_calls": msg.get("tool_calls"),
             "finish_reason": (collected.get("choices") or [{}])[0].get("finish_reason"),
             "usage": collected.get("usage"), "model": collected.get("model")}
        for line in _chat_result_to_sse_lines(m):
            yield (line + "\n\n").encode("utf-8")
        elapsed = time.time() - t0 if t0 else 0
        tool_names = [tc["function"]["name"] for tc in m["tool_calls"] or []]
        tag = " ⚠️内容审核拦截" if _looks_like_content_filter_text(m["content"]) else ""
        _log(f"{prefix}◀ RESPONSE {model_name} | {elapsed:.1f}s | stream-agg finish={m['finish_reason'] or 'stop'}{tag}"
             + (f" | tool_calls={tool_names}" if tool_names else "")
             + f" | tokens={(m['usage'] or {}).get('total_tokens', '?')}")
        _log(f"{prefix}── RESPONSE BODY ──\n{json.dumps(collected, ensure_ascii=False, indent=2)}")
        return

    def _feed(chunk: bytes):
        nonlocal finish_reason, saw_filter, buf
        # 行缓冲解析：把累计的 chunk 按 data: 行切出来统计
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                continue
            try:
                obj = json.loads(data)
            except Exception:
                continue
            if obj.get("usage"):
                usage.update(obj["usage"])
            for ch in obj.get("choices") or []:
                if ch.get("finish_reason"):
                    finish_reason = ch["finish_reason"]
                for tc in (ch.get("delta") or {}).get("tool_calls") or []:
                    nm = (tc.get("function") or {}).get("name")
                    if nm:
                        tool_names.append(nm)
            # 内容审核拦截常以 content-filter 或特殊中文文案返回
            try:
                text_repr = data.decode("utf-8", "replace")
            except Exception:
                text_repr = ""
            if "content-filter" in text_repr or "敏感" in text_repr or "审核" in text_repr:
                saw_filter = True

    produced = False  # 已向下游产出过内容：断流后不再重试（避免重复内容/重复计费）
    for attempt in range(_STREAM_CONN_RETRY + 1):
        try:
            async with httpx.AsyncClient(timeout=None) as c:
                async with c.stream("POST", url, headers=headers, json=body) as r:
                    if r.status_code != 200:
                        err = await r.aread()
                        _note_cred_status(cred, r.status_code, model=body.get("model"), raw=err)
                        _log(f"{prefix}✗ HTTP {r.status_code} | {model_name} | {_truncate(err.decode('utf-8','replace'),200)}")
                        _log(f"{prefix}── ERROR BODY ──\n{err.decode('utf-8','replace')}")
                        yield _err_event(err, r.status_code)
                        return
                    async for chunk in r.aiter_bytes():
                        if chunk:
                            produced = True
                            raw_parts.append(chunk)
                            _feed(chunk)
                            yield chunk
            break  # 流正常结束
        except httpx.HTTPError as e:
            if not produced and attempt < _STREAM_CONN_RETRY:
                _log(f"{prefix}✗ 网络错误（未产出任何内容），整请求重试 {attempt + 1}/{_STREAM_CONN_RETRY} | {model_name} | {e}")
                continue
            _log(f"{prefix}✗ 网络错误 | {model_name} | {e}")
            yield _err_event(str(e).encode(), 502)
            break

    # 流结束：输出完成日志
    elapsed = time.time() - t0 if t0 else 0
    tag = " ⚠️内容审核拦截" if (saw_filter or finish_reason == "content-filter") else ""
    _log(f"{prefix}◀ RESPONSE {model_name} | {elapsed:.1f}s | stream finish={finish_reason}{tag}"
         + (f" | tool_calls={tool_names}" if tool_names else "")
         + f" | tokens={usage.get('total_tokens', '?')}")
    # 完整原始 SSE（后端返回的全部内容）
    _log(f"{prefix}── RESPONSE RAW SSE ──\n{b''.join(raw_parts).decode('utf-8','replace')}")


def _safe_err(r: httpx.Response) -> dict:
    try:
        return {"error": r.json()}
    except Exception:
        return {"error": {"message": r.text[:500], "type": "upstream_error", "code": r.status_code}}


def _err_event(msg: bytes, status: int) -> bytes:
    # 以 OpenAI SSE 错误 chunk 形式返回
    import json as _json, time as _time
    chunk = {
        "error": {"message": msg.decode("utf-8", "replace")[:500], "type": "upstream_error", "code": status},
    }
    return f"data: {_json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")


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


async def _post_backend_once(url: str, headers: dict, body: dict) -> tuple[int, bytes]:
    async with httpx.AsyncClient(timeout=120) as c:
        async with c.stream("POST", url, headers=headers, json=body) as r:
            chunks: list[bytes] = []
            async for chunk in r.aiter_bytes():
                if chunk:
                    chunks.append(chunk)
            return r.status_code, b"".join(chunks)


async def _post_backend_with_filter_retry(url: str, headers: dict, body: dict,
                                          rid: str = "", model_name: str = "?") -> tuple[int, bytes, dict]:
    prefix = f"[{rid}] " if rid else ""
    status, raw = await _post_backend_once(url, headers, body)
    text = raw.decode("utf-8", "replace")
    if status == 200 and _looks_like_content_filter_text(text) and CONFIG.get("desensitize") and CONFIG.get("no_compact"):
        retry_body = _chat_body_desensitize(body, force_compact=True)
        _log(f"{prefix}↻ RESPONSES {model_name} | content filter detected, retry with compact harness")
        _log(f"{prefix}── RESPONSES RETRY CHAT BODY ──\n{json.dumps(retry_body, ensure_ascii=False, indent=2)}")
        retry_status, retry_raw = await _post_backend_once(url, headers, retry_body)
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

    # 转换请求：Responses → Chat
    try:
        chat_body = responses_request_to_chat(payload)
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"request conversion error: {e}", "type": "invalid_request_error"}})

    chat_body, projection_stats = project_responses_chat_body(chat_body)
    chat_body.setdefault("model", "auto")
    guard_model(chat_body["model"])
    chat_body["stream"] = True
    if "stream_options" not in chat_body:
        chat_body["stream_options"] = {"include_usage": True}

    chat_body = _chat_body_desensitize(chat_body)

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
    _log(f"[{rid}] ── RESPONSES → CHAT BODY ──\n{json.dumps(chat_body, ensure_ascii=False, indent=2)}")

    cred, headers = _cred_for(payload, chat_body.get("model"))
    url = f"{BACKEND}/v2/chat/completions"
    t0 = time.time()

    if client_wants_stream:
        return StreamingResponse(
            _stream_responses(url, headers, chat_body, model_name, t0, rid, cred=cred),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式：聚合后端 SSE → 非流式 Response 对象
    try:
        status_code, raw, final_body = await _post_backend_with_filter_retry(url, headers, chat_body, rid, model_name)
        if status_code != 200:
            _note_cred_status(cred, status_code, model=chat_body.get("model"), raw=raw)
            _log(f"[{rid}] ✗ HTTP {status_code} | {model_name} | {_truncate(raw.decode('utf-8','replace'),200)}")
            raise HTTPException(status_code=status_code, detail=_safe_err_raw(raw, status_code))
        converter = ResponsesStreamConverter(model=model_name)
        for line in raw.decode("utf-8", "replace").splitlines():
            converter.feed_line(line)
        chat_body = final_body
    except HTTPException:
        raise
    except httpx.HTTPError as e:
        _log(f"[{rid}] ✗ 网络错误 | {model_name} | {e}")
        raise HTTPException(status_code=502, detail={"error": {"message": f"upstream error: {e}", "type": "upstream_error"}})

    result = converter.get_nonstream_response()
    elapsed = time.time() - t0
    _log(f"[{rid}] ◀ RESPONSES {model_name} | {elapsed:.1f}s")
    _log(f"[{rid}] ── RESPONSE OBJ ──\n{json.dumps(result, ensure_ascii=False, indent=2)}")
    return JSONResponse(content=result)


async def _stream_responses(url: str, headers: dict, body: dict,
                            model_name: str = "?", t0: float = 0.0, rid: str = "", cred=None):
    """消费后端 Chat SSE，实时转换为 Responses API 事件流输出。"""
    converter = ResponsesStreamConverter(model=model_name)
    prefix = f"[{rid}] " if rid else ""

    try:
        # 带 tools 时聚合校验 tool_calls，损坏重试（后端流式分片偶发损坏）
        for attempt in range(_TOOL_CALL_MAX_RETRY + 1):
            try:
                status_code, raw, _ = await _post_backend_with_filter_retry(url, headers, body, rid, model_name)
            except httpx.HTTPError as e:  # 聚合调用未产出过内容，网络错误可安全重试
                if attempt < _TOOL_CALL_MAX_RETRY:
                    _log(f"{prefix}✗ 网络错误（未产出），重试 {attempt + 1}/{_TOOL_CALL_MAX_RETRY} | {model_name} | {e}")
                    continue
                raise
            if status_code != 200:
                _note_cred_status(cred, status_code, model=body.get("model"), raw=raw)
                _log(f"{prefix}✗ HTTP {status_code} | {model_name} | {_truncate(raw.decode('utf-8','replace'),200)}")
                error_evt = {"type": "error", "error": {"message": raw.decode('utf-8','replace')[:500], "code": status_code}}
                yield f"data: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode("utf-8")
                return
            if not body.get("tools"):
                break
            merged = _merge_chat_sse_text(raw.decode("utf-8", "replace"))
            if _tool_calls_healthy(merged["tool_calls"]):
                break
            _log(f"{prefix}▸ 流式 tool_calls 损坏（name 空/参数无效），重试 {attempt + 1}/{_TOOL_CALL_MAX_RETRY}")
        raw_sse_lines = []
        for line in raw.decode("utf-8", "replace").splitlines():
            if line.strip():
                raw_sse_lines.append(line)
            events = converter.feed_line(line)
            if events:
                yield events.encode("utf-8")
    except httpx.HTTPError as e:
        _log(f"{prefix}✗ 网络错误 | {model_name} | {e}")
        error_evt = {"type": "error", "error": {"message": str(e)[:500], "code": 502}}
        yield f"data: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode("utf-8")
        return

    # 发送收尾事件
    finish_events = converter.finish()
    if finish_events:
        yield finish_events.encode("utf-8")

    elapsed = time.time() - t0 if t0 else 0
    _log(f"{prefix}◀ RESPONSES {model_name} | {elapsed:.1f}s | stream done")
    _log(f"{prefix}── RESPONSES RAW SSE ──\n" + "\n".join(raw_sse_lines[-30:]))


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

    # 将 Anthropic 格式消息、工具规范在进入后端前统一转换为 OpenAI Chat 格式。
    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {"message": "messages is required", "type": "invalid_request_error"}})

    try:
        chat_body = anthropic_request_to_chat(payload)
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"request conversion error: {e}", "type": "invalid_request_error"}})

    chat_body.setdefault("model", "auto")
    guard_model(chat_body["model"])
    chat_body["stream"] = True
    if "stream_options" not in chat_body:
        chat_body["stream_options"] = {"include_usage": True}

    if CONFIG.get("desensitize"):
        chat_body = desensitize_body(chat_body, roles=("system", "developer"),
                                     desensitize_harness_user=True,
                                     desensitize_tools=True,
                                     compact_harness=not CONFIG.get("no_compact"),
                                     strip_tool_metadata=True)

    model_name = payload.get("model", "auto")
    chat_messages = chat_body.get("messages", [])
    rid = os.urandom(4).hex()
    _log(f"[{rid}] ▶ ANTHROPIC {model_name} | msgs={len(chat_messages)} | anthropic_msgs={len(messages)}")
    _log(f"[{rid}] ── ANTHROPIC → CHAT BODY ──\n{json.dumps(chat_body, ensure_ascii=False, indent=2)}")

    cred, headers = _cred_for(payload, chat_body.get("model"))
    url = f"{BACKEND}/v2/chat/completions"
    t0 = time.time()

    return StreamingResponse(
        _stream_anthropic(url, headers, chat_body, model_name, t0, rid, cred=cred),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _stream_anthropic(url: str, headers: dict, body: dict,
                            model_name: str = "?", t0: float = 0.0, rid: str = "", cred=None):
    """消费后端 OpenAI Chat SSE，实时转换为 Anthropic Messages SSE 事件流。"""
    converter = AnthropicStreamConverter(model=model_name)
    prefix = f"[{rid}] " if rid else ""

    # 带 tools 时聚合校验 tool_calls，损坏重试后伪流式喂给转换器
    if body.get("tools"):
        merged: dict = {}
        for attempt in range(_TOOL_CALL_MAX_RETRY + 1):
            try:
                async with httpx.AsyncClient(timeout=None) as c:
                    async with c.stream("POST", url, headers=headers, json=body) as r:
                        if r.status_code != 200:
                            err = await r.aread()
                            _note_cred_status(cred, r.status_code, model=body.get("model"), raw=err)
                            _log(f"{prefix}✗ HTTP {r.status_code} | {model_name} | {_truncate(err.decode('utf-8','replace'),200)}")
                            error_evt = {"type": "error", "error": {"message": err.decode('utf-8','replace')[:500], "type": "api_error", "code": r.status_code}}
                            yield f"event: error\ndata: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode("utf-8")
                            return
                        merged = _merge_chat_sse_text((await r.aread()).decode("utf-8", "replace"))
            except httpx.HTTPError as e:
                if attempt < _TOOL_CALL_MAX_RETRY:  # 聚合分支未产出过内容，网络错误可安全重试
                    _log(f"{prefix}✗ 网络错误（未产出），重试 {attempt + 1}/{_TOOL_CALL_MAX_RETRY} | {model_name} | {e}")
                    continue
                _log(f"{prefix}✗ 网络错误 | {model_name} | {e}")
                error_evt = {"type": "error", "error": {"message": str(e)[:500], "type": "api_error", "code": 502}}
                yield f"event: error\ndata: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode("utf-8")
                return
            if _tool_calls_healthy(merged["tool_calls"]):
                break
            _log(f"{prefix}▸ 流式 tool_calls 损坏（name 空/参数无效），重试 {attempt + 1}/{_TOOL_CALL_MAX_RETRY}")
        for line in _chat_result_to_sse_lines(merged):
            events = converter.feed_line(line)
            if events:
                yield events.encode("utf-8")
        finish_events = converter.finish()
        if finish_events:
            yield finish_events.encode("utf-8")
        elapsed = time.time() - t0 if t0 else 0
        _log(f"{prefix}◀ ANTHROPIC {model_name} | {elapsed:.1f}s | stream-agg done")
        return

    produced = False  # 已向下游产出过事件：断流后不再重试（避免重复内容/重复计费）
    for attempt in range(_STREAM_CONN_RETRY + 1):
        if attempt:
            converter = AnthropicStreamConverter(model=model_name)  # 重试：重建转换器避免脏状态
        try:
            async with httpx.AsyncClient(timeout=None) as c:
                async with c.stream("POST", url, headers=headers, json=body) as r:
                    if r.status_code != 200:
                        err = await r.aread()
                        _note_cred_status(cred, r.status_code, model=body.get("model"), raw=err)
                        _log(f"{prefix}✗ HTTP {r.status_code} | {model_name} | {_truncate(err.decode('utf-8','replace'),200)}")
                        error_evt = {"type": "error", "error": {"message": err.decode('utf-8','replace')[:500], "type": "api_error", "code": r.status_code}}
                        yield f"event: error\ndata: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode("utf-8")
                        return
                    async for line in r.aiter_lines():
                        events = converter.feed_line(line)
                        if events:
                            produced = True
                            yield events.encode("utf-8")
            break  # 流正常结束
        except httpx.HTTPError as e:
            if not produced and attempt < _STREAM_CONN_RETRY:
                _log(f"{prefix}✗ 网络错误（未产出任何内容），整请求重试 {attempt + 1}/{_STREAM_CONN_RETRY} | {model_name} | {e}")
                continue
            _log(f"{prefix}✗ 网络错误 | {model_name} | {e}")
            error_evt = {"type": "error", "error": {"message": str(e)[:500], "type": "api_error", "code": 502}}
            yield f"event: error\ndata: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode("utf-8")
            return

    finish_events = converter.finish()
    if finish_events:
        yield finish_events.encode("utf-8")

    elapsed = time.time() - t0 if t0 else 0
    _log(f"{prefix}◀ ANTHROPIC {model_name} | {elapsed:.1f}s | stream done")


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
        sys.stderr.write("\n[警告] 未找到登录文件。请在桌面端完成登录（CodeBuddy/WorkBuddy），或用 --auth-file 指定。\n")
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


def main():
    ap = argparse.ArgumentParser(description="CodeBuddy -> OpenAI 兼容转换器（直连后端）")
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
    args = ap.parse_args()

    CONFIG["api_key"] = args.api_key
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
    threading.Thread(target=_refresher_loop, args=(CONFIG["cred_pool"],),
                     daemon=True, name="cred-refresher").start()
    if credits_mod is not None:
        ledger = credits_mod.CreditLedger(managed_auth_dir() / "credits-ledger.json")
        CONFIG["ledger"] = ledger
        CONFIG["model_cache"] = credits_mod.ModelCatalogCache(
            managed_auth_dir() / "model-catalog.json", ttl=args.model_catalog_ttl)
        # 启动即用本地缓存对外提供模型表，无需等首轮云端同步
        CONFIG["models_remote"] = CONFIG["model_cache"].models("domestic") or None
        CONFIG["models_intl"] = CONFIG["model_cache"].models("international") or None
        CONFIG["cred_pool"].set_ledger(ledger)  # pick 按积分最早过期时间优先
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
    sys.stderr.write("   POST /admin/oauth/start + GET /admin/oauth/poll  (无感登录采集新凭证)\n")
    if credits_mod is not None:
        sys.stderr.write("   GET  /admin/credits           (积分/签到状态)\n")
        sys.stderr.write("   POST /admin/checkin           (手动触发签到+积分刷新)\n")
        sys.stderr.write("   每日签到 + 快过期积分优先调度已启用\n")
    if args.api_key:
        sys.stderr.write("   鉴权已启用（API key 已设置）\n")
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
    main()
