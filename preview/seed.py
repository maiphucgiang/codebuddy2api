"""Synthetic accounts and request history for the preview gateway; nothing here is a real identity."""

from dataclasses import dataclass, field
import json
import random
import struct
import time
import uuid
import zlib
from pathlib import Path

from app.audit_store import AuditStore
from app.client_profiles import account_key
from app.credential_io import atomic_write_credential
from app.site_routing import PROFILE_DOMAINS

DAY = 86400


@dataclass(frozen=True)
class Account:
    key: str
    profile: str
    uid: str
    nickname: str
    # (name, code, remaining, total, days until expiry)
    packages: tuple = ()
    enterprise: str = ""
    # (limit, used); a limit of -1 is an unlimited tenant.
    allowance: tuple = (0, 0)
    # A revoked refresh token leaves the account visibly signed out.
    revoked: bool = False
    daily_requests: int = 6
    models: tuple = field(default=())
    media: tuple = ()


CN_MODELS = ("glm-5.1", "deepseek-v3.2", "kimi-k2", "claude-sonnet-4.6", "gpt-5.1")
INTL_MODELS = ("claude-sonnet-4.6", "gpt-5.1", "gemini-2.5-pro")

# Media models advertised by personal domestic CLI accounts; image models never join chat.
CN_MEDIA_ALL = ("hunyuan-image-v3.0", "hunyuan-image-v3.0-art", "hy-image-3.5",
                "gemini-3.0-pro-image", "hunyuan-image-v2.0-general-edit", "minimax-video-h3",
                "hunyuan-video-art")
CN_MEDIA_MIN = ("hunyuan-image-v3.0", "minimax-video-h3")

# The simulated COS bucket the media endpoints hand out signed URLs for.
COS_HOST = "cos.preview.invalid"
SEED_VIDEO_TASK_ID = "previewvideotask0000000000000001"



def _png(size, rgb) -> bytes:
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


PNG_BYTES = _png(48, (0x7C, 0x66, 0xDC))
PNG_BYTES_ALT = _png(48, (0x2A, 0x9D, 0x8F))
# A minimal ftyp-branded file: the store only sniffs headers, and the demo never plays it.
MP4_BYTES = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00" * 4096

ACCOUNTS = (
    Account("aurora", "cn-cli", "100001", "Aurora", daily_requests=14, models=CN_MODELS,
            media=CN_MEDIA_ALL, packages=(
        ("个人版订阅套餐", "plan-month", 1500, 2000, 18), ("签到奖励", "checkin", 24.5, 30, 3))),
    Account("breeze", "cn-work", "100002", "Breeze", daily_requests=8, models=CN_MODELS, packages=(
        ("新人权益包", "welcome", 600, 1000, 9), ("旅行奖励", "travel", 260, 300, 26))),
    Account("cedar", "cn-cli", "100003", "Cedar", daily_requests=3, models=CN_MODELS,
            media=CN_MEDIA_MIN, packages=(
        ("活动奖励", "event", 42, 200, 2),)),
    Account("dune", "intl-cli", "200001", "Dune", daily_requests=6, models=INTL_MODELS, packages=(
        ("Pro plan", "pro", 420, 500, 21),)),
    Account("ember", "intl-work", "200002", "Ember", daily_requests=9, models=INTL_MODELS, packages=(
        ("Subscription", "subscription", 1260, 2000, 27), ("Check-in bonus", "bonus", 15, 15, 5))),
    Account("fjord", "cn-cli", "300001", "Fjord", enterprise="tenant-fjord", allowance=(-1, 0),
            daily_requests=11, models=CN_MODELS),
    Account("grove", "cn-work", "300002", "Grove", enterprise="tenant-grove", allowance=(5000, 1830),
            daily_requests=7, models=CN_MODELS),
    Account("harbor", "cn-cli", "100004", "Harbor", revoked=True, daily_requests=0, models=CN_MODELS,
            packages=(("个人版订阅套餐", "plan-month", 880, 2000, 12),)),
)


def token(account: Account, generation: int = 0) -> str:
    # No dots: a three-part token would be read as a JWT routing hint.
    return f"preview-{account.key}-{generation}"


def credential(account: Account, now: float) -> dict:
    profile = {"uid": account.uid, "nickname": account.nickname}
    if account.enterprise:
        profile.update(enterpriseId=account.enterprise, enterpriseName=f"{account.nickname} Studio",
                       type="ultimate")
    expires = now - 3600 if account.revoked else now + 6 * 3600
    return {"account": profile,
            "auth": {"domain": PROFILE_DOMAINS[account.profile], "accessToken": token(account),
                     "refreshToken": f"preview-{account.key}-refresh", "expiresAt": int(expires * 1000)}}


def write_credentials(directory, now: float | None = None):
    now = time.time() if now is None else now
    for account in ACCOUNTS:
        atomic_write_credential(directory, f"{account.key}.info",
                                json.dumps(credential(account, now), ensure_ascii=False, indent=2).encode())


def write_history(path, now: float | None = None, days: int = 30, seed: int = 7):
    """Backfill request records so the dashboard and logs open with a month of traffic."""
    now = time.time() if now is None else now
    rng = random.Random(seed)
    store = AuditStore(path)
    try:
        for day in range(days, -1, -1):
            # Weekdays are busier, and traffic grows slowly towards today.
            weekday = time.localtime(now - day * DAY).tm_wday < 5
            scale = (1.0 if weekday else 0.45) * (0.6 + 0.4 * (days - day) / days)
            for account in ACCOUNTS:
                for _ in range(round(account.daily_requests * scale * rng.uniform(0.6, 1.4))):
                    started = now - day * DAY - rng.uniform(0, DAY if day else 6 * 3600)
                    store.record_request(_record(account, started, rng))
    finally:
        store.close()


def _record(account: Account, started: float, rng: random.Random) -> dict:
    model = rng.choice(account.models)
    protocol = rng.choices(("chat", "responses", "messages"), (5, 3, 4))[0]
    failed = rng.random() < 0.04
    input_tokens = int(rng.lognormvariate(8.2, 0.9))
    output_tokens = 0 if failed else int(rng.lognormvariate(6.2, 0.8))
    cache = int(input_tokens * rng.uniform(0.3, 0.85)) if rng.random() < 0.7 else 0
    identity = account_key(account.profile, account.uid, account.enterprise)
    duration = int(rng.lognormvariate(8.3, 0.6))
    status = rng.choice((429, 500, 503)) if failed else 200
    return {
        "id": uuid.UUID(int=rng.getrandbits(128)).hex, "started_at": started,
        "model": model, "upstream_model": model, "profile": account.profile, "credential": identity,
        "protocol": protocol, "streaming": rng.random() < 0.8, "outcome": "error" if failed else "success",
        "status_code": status, "error_code": "upstream_error" if failed else None,
        "duration_ms": duration, "first_token_ms": None if failed else int(duration * rng.uniform(0.1, 0.35)),
        "input_tokens": input_tokens, "output_tokens": output_tokens, "cache_read_tokens": cache,
        "reasoning_tokens": int(output_tokens * rng.uniform(0.1, 0.4)) if "claude" in model else 0,
        "total_tokens": input_tokens + output_tokens,
        "credit": 0 if failed else round((input_tokens + output_tokens) / 1000 * rng.uniform(0.4, 1.6), 2),
        "client_ip": "127.0.0.1", "user_agent": rng.choice(("claude-cli/2.1", "codex_cli_rs/0.46", "OpenAI/Python")),
        "attempts": [{"outcome": "error" if failed else "success", "credential": identity,
                      "status_code": status, "duration_ms": duration}],
    }


def write_media(directory, now: float | None = None):
    """Seed local media assets and video tasks so the dashboard media page opens populated."""
    from app.media_store import MediaStore

    now = time.time() if now is None else now
    store = MediaStore(Path(directory))
    try:
        quota = {"ttl_hours": 72, "max_total_bytes": 1 << 30}
        store.save_asset("image", PNG_BYTES, model="hunyuan-image-v3.0", **quota)
        # A second image a moment older keeps the newest-first listing meaningful.
        first = store.save_asset("image", PNG_BYTES_ALT, model="hy-image-3.5", **quota)
        store._db.execute("UPDATE assets SET created=? WHERE id=?", (now - 3600, first["id"]))
        store._db.commit()
        video = store.save_asset("video", MP4_BYTES, model="minimax-video-h3", **quota)
        aurora = account_key("cn-cli", "100001", "")
        store.create_task("seedvideotask-complete", aurora, "cn-cli", "minimax-video-h3")
        store.update_task("seedvideotask-complete", status="completed",
                          media_id=video["id"], credit=12.5)
        # Stays in progress until someone refreshes it; the simulated upstream knows this id.
        store.create_task(SEED_VIDEO_TASK_ID, aurora, "cn-cli", "hunyuan-video-art")
    finally:
        store.close()
