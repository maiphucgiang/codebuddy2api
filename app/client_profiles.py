"""Build separate CLI and WorkBuddy identity headers with shared authentication fields."""

import base64
import binascii
import hashlib
import json
import uuid

from .site_routing import domain_for_auth, profile_for_auth, profile_product, profile_region

CLI_VERSION = "2.165.0"
WORKBUDDY_VERSION = "5.9.3"
WORKBUDDY_CLI_VERSION = CLI_VERSION
INTL_CLI_VERSION = "2.161.0"
INTL_WORKBUDDY_VERSION = "5.8.4"
INTL_WORKBUDDY_CLI_VERSION = INTL_CLI_VERSION
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


def _versions(profile):
    if profile_region(profile) == "intl":
        return INTL_CLI_VERSION, INTL_WORKBUDDY_VERSION, INTL_WORKBUDDY_CLI_VERSION
    return CLI_VERSION, WORKBUDDY_VERSION, WORKBUDDY_CLI_VERSION


def identity_headers(profile: str) -> dict:
    """Use verified package versions without reading desktop settings or device identities."""
    cli_version, workbuddy_version, workbuddy_cli_version = _versions(profile)
    if profile_product(profile) == "cli":
        return {"User-Agent": f"CLI/{cli_version} CodeBuddy/{cli_version}",
                "X-IDE-Type": "CLI", "X-IDE-Name": "CLI", "X-IDE-Version": cli_version}
    name = "WorkBuddy AI" if profile_region(profile) == "intl" else "WorkBuddy"
    return {
        "User-Agent": f"WorkBuddy/{workbuddy_version} {name}/{workbuddy_version} CLI/{workbuddy_cli_version}",
        "X-IDE-Type": "WorkBuddy", "X-IDE-Name": "WorkBuddy", "X-IDE-Version": workbuddy_version,
        "X-Private-Data": "true",
    }


def _auth_headers(auth: dict, account: dict) -> dict:
    # Read the tenant id through the shared helper so header and billing dispatch agree.
    enterprise_id = enterprise_id_of(account) or ""
    headers = {
        "Content-Type": "application/json", "Accept": "application/json",
        "Authorization": f"Bearer {auth.get('accessToken', '')}",
        "X-User-Id": str(account.get("uid") or ""),
        "X-Enterprise-Id": enterprise_id,
        "X-Tenant-Id": enterprise_id,
        "X-Domain": domain_for_auth(auth),
        "X-Product": "SaaS", "X-Requested-With": "XMLHttpRequest",
    }
    profile = profile_for_auth(auth)
    headers.update(identity_headers(profile))
    if profile in ("cn-cli", "cn-work"):
        # CLI 2.165+ disables model optimization for enterprise accounts.
        headers["X-Private-Data"] = "true" if is_enterprise_account(account) else "false"
    return headers


def _jwt_claims(auth: dict) -> dict:
    """Read unsigned claims only as client metadata; upstream still authenticates the token."""
    token = auth.get("accessToken")
    if not isinstance(token, str) or token.count(".") != 2:
        return {}
    try:
        encoded = token.split(".")[1]
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(decoded)
    except (ValueError, binascii.Error, UnicodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _identity_value(value) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value if value and len(value) <= 128 and value.isascii() and value.isprintable() else ""


def _identity_claim_headers(auth: dict, account: dict) -> dict:
    claims = _jwt_claims(auth)
    id_source = _identity_value(account.get("idSource") or account.get("id_source") or claims.get("id_source"))
    token_source = _identity_value(claims.get("token_source") or auth.get("method"))
    enterprise_id = enterprise_id_of(account) or ""
    uid = canonical_uid(account)
    headers = {}
    if id_source:
        headers["X-Id-Source"] = id_source
    if token_source:
        headers["X-Auth-Method"] = token_source
    if uid and (enterprise_id or id_source or token_source):
        userinfo = {"uin": uid}
        if enterprise_id:
            userinfo["owner_uin"] = enterprise_id
        if id_source:
            userinfo["id_source"] = id_source
        if token_source:
            userinfo["token_source"] = token_source
        encoded = json.dumps(userinfo, separators=(",", ":")).encode("utf-8")
        headers["X-Userinfo"] = base64.b64encode(encoded).decode("ascii")
    return headers


def credential_headers(auth: dict, account: dict) -> dict:
    return {**SDK_HEADERS, **_auth_headers(auth, account), **_identity_claim_headers(auth, account)}


def _cn_work_nonchat_headers(headers: dict, profile: str) -> dict:
    if profile == "cn-work":
        headers["Accept"] = "application/json, text/plain, */*"
        headers["X-Request-ID"] = uuid.uuid4().hex
        headers.pop("X-CodeBuddy-Request", None)
    return headers


def credential_refresh_headers(auth: dict, account: dict) -> dict:
    headers = credential_headers(auth, account)
    return _cn_work_nonchat_headers(headers, profile_for_auth(auth))


def refresh_headers(domain: str, uid: str = "") -> dict:
    """Build headers for a refresh exchange that has no access token yet."""
    auth = {"accessToken": "", "domain": domain}
    headers = {**SDK_HEADERS, **_auth_headers(auth, {"uid": uid})}
    headers.pop("Authorization", None)  # The refresh token is the only credential in this request.
    return _cn_work_nonchat_headers(headers, profile_for_auth(auth))


def catalog_headers(auth: dict, account: dict | None = None, *, user_agent="") -> dict:
    profile = profile_for_auth(auth)
    headers = _cn_work_nonchat_headers(_auth_headers(auth, account or {}), profile)
    headers["Connection"] = "close"
    if profile_product(profile) == "cli":
        headers["x-client-platform"] = "cli"
        if user_agent:
            headers["User-Agent"] = user_agent
    # A CLI platform header would select the wrong product catalog for WorkBuddy.
    return headers


def account_key(profile: str, uid, enterprise_id="") -> str:
    """Hash product, account and tenant identity without tokens or local paths."""
    identity = json.dumps([profile, str(uid or ""), str(enterprise_id or "")],
                          ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def enterprise_id_of(account) -> str | None:
    """Read the tenant identifier in either naming convention.

    The official clients emit ``enterpriseId`` from the auth payload and
    ``enterprise_id`` in some backup shapes; both must resolve to one value so the
    billing dispatch and the request headers never disagree.
    """
    if not isinstance(account, dict):
        return None
    for field in ("enterpriseId", "enterprise_id"):
        value = account.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def is_enterprise_account(account) -> bool:
    """Whether the account belongs to an enterprise tenant.

    An enterprise id is a direct hit. Historical backups may carry only
    ``type in {ultimate, exclusive}`` without the id, but those accounts still bill
    through the enterprise endpoint, so the type is a fallback signal.
    """
    if enterprise_id_of(account) is not None:
        return True
    if not isinstance(account, dict):
        return False
    account_type = account.get("type")
    return isinstance(account_type, str) and account_type.strip().lower() in ("ultimate", "exclusive")


def normalize_uid(value) -> str:
    """Coerce a uid to its canonical string form, treating blanks as absent.

    A numeric uid is a real identity: it must hash the same as its string form and
    must never be mistaken for a missing uid, which would let two different
    accounts collapse onto one key.
    """
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # Reject non-finite and fractional values: they are not account identifiers.
        return str(int(value)) if value.is_integer() else ""
    if isinstance(value, str):
        return value.strip()
    return ""


# Display placeholders that must never be used as an identity.
_PLACEHOLDER_IDENTITIES = frozenset({"unknown", "手动添加", "未命名账号"})


def identity_email(account) -> str | None:
    """Return a real email usable as an identity when no uid exists.

    Placeholders, and values that merely repeat another field, are rejected: they
    would merge unrelated accounts onto a single key.
    """
    if not isinstance(account, dict):
        return None
    email = account.get("email")
    if not isinstance(email, str):
        return None
    text = email.strip()
    if not text or "@" not in text or text.lower() in _PLACEHOLDER_IDENTITIES:
        return None
    if text == normalize_uid(account.get("uid")):
        return None
    nickname = account.get("nickname")
    if isinstance(nickname, str) and text == nickname.strip():
        return None
    return text.lower()


def canonical_uid(account) -> str:
    """The identity uid for an account block, falling back to a real email.

    Every caller that derives an identity (import validation, hashing, sync
    reconciliation, duplicate detection) must use this one function so the keys
    they compare are always the same key.
    """
    return normalize_uid((account or {}).get("uid") if isinstance(account, dict) else None) \
        or (identity_email(account) or "")


def catalog_cache_key(profile: str, identity: str | None = None) -> str:
    cli_version, workbuddy_version, workbuddy_cli_version = _versions(profile)
    revision = cli_version if profile_product(profile) == "cli" else f"{workbuddy_version}:{workbuddy_cli_version}"
    key = f"{profile}:{revision}"
    return f"{key}:account:{identity}" if identity is not None else key
