"""CLI 与 WorkBuddy 产品身份头分开生成，共享认证字段与底层协议头。"""

import hashlib
import json

from .site_routing import domain_for_auth, profile_for_auth, profile_product, profile_region

CLI_VERSION = "2.149.0"
WORKBUDDY_VERSION = "5.5.2"
WORKBUDDY_CLI_VERSION = "2.137.1"
CLI_USER_AGENT = f"CLI/{CLI_VERSION} CodeBuddy/{CLI_VERSION}"

SDK_HEADERS = {
    "x-stainless-arch": "x64",
    "x-stainless-lang": "js",
    "x-stainless-os": "Linux",
    "x-stainless-package-version": "6.25.0",
    "x-stainless-retry-count": "0",
    "x-stainless-runtime": "node",
    "x-stainless-runtime-version": "v24.21.0",
    "X-Agent-Intent": "craft",
    "X-Agent-Purpose": "conversation",
    "X-Agent-Type": "main",
    "X-Private-Data": "false",
    "X-CodeBuddy-Request": "1",
}


def identity_headers(profile: str) -> dict:
    """版本来自已核对的官方包，不读取本机桌面用户配置或机器标识。"""
    if profile_product(profile) == "cli":
        return {"User-Agent": CLI_USER_AGENT, "X-IDE-Type": "CLI", "X-IDE-Name": "CLI", "X-IDE-Version": CLI_VERSION}
    name = "WorkBuddy AI" if profile_region(profile) == "intl" else "WorkBuddy"
    return {
        "User-Agent": f"WorkBuddy/{WORKBUDDY_VERSION} {name}/{WORKBUDDY_VERSION} CLI/{WORKBUDDY_CLI_VERSION}",
        "X-IDE-Type": "WorkBuddy", "X-IDE-Name": "WorkBuddy", "X-IDE-Version": WORKBUDDY_VERSION,
    }


def _auth_headers(auth: dict, account: dict) -> dict:
    headers = {
        "Content-Type": "application/json", "Accept": "application/json",
        "Authorization": f"Bearer {auth.get('accessToken', '')}",
        "X-User-Id": str(account.get("uid") or ""),
        "X-Enterprise-Id": str(account.get("enterpriseId") or ""),
        "X-Tenant-Id": str(account.get("enterpriseId") or ""),
        "X-Domain": domain_for_auth(auth),
        "X-Product": "SaaS", "X-Requested-With": "XMLHttpRequest",
    }
    headers.update(identity_headers(profile_for_auth(auth)))
    return headers


def credential_headers(auth: dict, account: dict) -> dict:
    return {**SDK_HEADERS, **_auth_headers(auth, account)}


def catalog_headers(auth: dict, account: dict | None = None, *, user_agent="") -> dict:
    headers = _auth_headers(auth, account or {})
    headers["Connection"] = "close"
    if profile_product(profile_for_auth(auth)) == "cli":
        headers["x-client-platform"] = "cli"
        if user_agent:
            headers["User-Agent"] = user_agent
    # WorkBuddy coordinator 不带 x-client-platform: cli，否则会取得另一产品的模型表。
    return headers


def account_key(profile: str, uid, enterprise_id="") -> str:
    """稳定产品/账号/租户指纹，不包含 token 或本地路径。"""
    identity = json.dumps([profile, str(uid or ""), str(enterprise_id or "")],
                          ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def catalog_cache_key(profile: str, identity: str | None = None) -> str:
    revision = CLI_VERSION if profile_product(profile) == "cli" else f"{WORKBUDDY_VERSION}:{WORKBUDDY_CLI_VERSION}"
    key = f"{profile}:{revision}"
    return f"{key}:account:{identity}" if identity is not None else key
