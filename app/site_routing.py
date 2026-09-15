"""Select fixed product/region endpoints from credential identity, never arbitrary input URLs."""

import base64
import binascii
import json
from urllib.parse import urlsplit

DOMESTIC = "domestic"
INTERNATIONAL = "international"
DOMESTIC_ENDPOINT = "https://copilot.tencent.com"
INTERNATIONAL_ENDPOINT = "https://www.codebuddy.ai"

PROFILE_ENDPOINTS = {
    "cn-cli": DOMESTIC_ENDPOINT,
    "cn-work": "https://www.workbuddy.cn",
    "intl-cli": INTERNATIONAL_ENDPOINT,
    "intl-work": "https://www.workbuddy.ai",
}
DOMAIN_PROFILES = {
    "www.codebuddy.cn": "cn-cli",
    "www.workbuddy.cn": "cn-work",
    "copilot.tencent.com": "cn-cli",
    "www.codebuddy.ai": "intl-cli",
    "www.workbuddy.ai": "intl-work",
}
# Verified CLI and WorkBuddy packages share these relative authentication paths.
_REFRESH_PATH = "/v2/plugin/auth/token/refresh"


def profile_region(profile: str) -> str:
    if profile not in PROFILE_ENDPOINTS:
        raise ValueError("Unknown credential profile")
    return profile.split("-", 1)[0]


def profile_product(profile: str) -> str:
    profile_region(profile)
    return "workbuddy" if profile.endswith("-work") else "cli"


def profile_site(profile: str) -> str:
    return INTERNATIONAL if profile_region(profile) == "intl" else DOMESTIC


def _known_host(value: str, *, issuer: bool = False) -> str:
    if not isinstance(value, str) or not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Invalid site hint")
    value = value.strip()
    if not value or any(char in value for char in "\\?#"):
        raise ValueError("Invalid site hint")
    try:
        parsed = urlsplit(value if "://" in value else "https://" + value)
        host = parsed.hostname
    except ValueError:
        raise ValueError("Invalid site hint") from None
    if (parsed.scheme.lower() != "https" or host not in DOMAIN_PROFILES
            or parsed.netloc.lower() != host or (not issuer and parsed.path not in ("", "/"))):
        raise ValueError("Unsupported site hint")
    return host


def normalize_domain(value: str) -> str:
    """Normalize known HTTPS hosts while rejecting userinfo, ports and arbitrary paths."""
    return _known_host(value)


def _token_issuer(token):
    if not isinstance(token, str) or token.count(".") != 2:
        return None
    payload = token.split(".")[1]
    try:
        claims = json.loads(base64.b64decode(payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True))
    except (ValueError, UnicodeError, binascii.Error, RecursionError):
        return None
    return claims.get("iss") if isinstance(claims, dict) else None


def _hints(auth):
    domain, issuer = auth.get("domain"), _token_issuer(auth.get("accessToken"))
    return (normalize_domain(domain) if domain not in (None, "") else None,
            _known_host(issuer, issuer=True) if issuer not in (None, "") else None)


def site_for_auth(auth: dict) -> str:
    """Use JWT claims only as routing hints; upstream validates signatures and account permissions."""
    domain, issuer = _hints(auth)
    sites = {profile_site(DOMAIN_PROFILES[host]) for host in (domain, issuer) if host}
    if len(sites) > 1:
        raise ValueError("Conflicting credential sites")
    return next(iter(sites), DOMESTIC)


def profile_for_auth(auth: dict) -> str:
    site_for_auth(auth)
    domain, issuer = _hints(auth)
    # Explicit account domains disambiguate the shared domestic copilot endpoint.
    branded = [host for host in (domain, issuer) if host and host != "copilot.tencent.com"]
    profiles = {DOMAIN_PROFILES[host] for host in branded}
    if len(profiles) > 1:
        raise ValueError("Conflicting credential products")
    return next(iter(profiles), "cn-cli")


def domain_for_auth(auth: dict) -> str:
    profile_for_auth(auth)
    domain, issuer = _hints(auth)
    return domain or issuer or "www.codebuddy.cn"


def _header(headers: dict, name: str):
    values = [value for key, value in headers.items() if str(key).lower() == name]
    if values and any(value != values[0] for value in values[1:]):
        raise ValueError("Conflicting routing headers")
    return values[0] if values else None


def _auth_from_headers(headers: dict) -> dict:
    authorization = _header(headers, "authorization")
    token = None
    if isinstance(authorization, str):
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1].strip()
    return {"domain": _header(headers, "x-domain"), "accessToken": token}


def site_for_headers(headers: dict) -> str:
    return site_for_auth(_auth_from_headers(headers))


def profile_for_headers(headers: dict) -> str:
    return profile_for_auth(_auth_from_headers(headers))


def chat_url_for_headers(headers: dict) -> str:
    return PROFILE_ENDPOINTS[profile_for_headers(headers)] + "/v2/chat/completions"


def refresh_url_for_auth(auth: dict) -> str:
    return PROFILE_ENDPOINTS[profile_for_auth(auth)] + _REFRESH_PATH
