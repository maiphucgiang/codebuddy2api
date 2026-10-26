"""Media generation contracts: model lists, request shaping and status mapping.

Pure data shaping shared by the gateway endpoints, the media store and the WebUI
administration API; nothing here touches FastAPI, the network or configuration.
"""
from __future__ import annotations

from urllib.parse import urlsplit

# Profiles verified against the paid media endpoints; extend only after live checks.
MEDIA_PROFILES = frozenset({"cn-cli"})

# Static families, listed model by model; account catalogs extend the validation set.
IMAGE_TEXT_MODELS = (
    "hunyuan-image-v3.0", "hunyuan-image-v3.0-art", "hunyuan-image-alpha", "hy-image-3.5",
    "gemini-3.0-pro-image", "gemini-3.1-flash-image", "gemini-2.5-flash-image")
IMAGE_EDIT_MODELS = (
    "hunyuan-image-v2.0-general-edit", "hunyuan-image-alpha-edit", "hunyuan-image-v3.0-art",
    "hy-image-3.5", "gemini-3.0-pro-image", "gemini-3.1-flash-image", "gemini-2.5-flash-image")
VIDEO_MODELS = ("minimax-video-h3", "hunyuan-video-art", "kling-video-v3")

# Fixed defaults: never derived from catalog order, which is unstable across accounts.
DEFAULT_IMAGE_MODEL = "hunyuan-image-v3.0"
DEFAULT_IMAGE_EDIT_MODEL = "hunyuan-image-v2.0-general-edit"
DEFAULT_VIDEO_MODEL = "minimax-video-h3"

GEMINI_IMAGE_MODELS = frozenset(("gemini-3.0-pro-image", "gemini-3.1-flash-image", "gemini-2.5-flash-image"))

MAX_IMAGE_ITEMS = 4          # Public n bound; the upstream contract allows 1..4.
VIDEO_RESOLUTIONS = ("768P", "2K")
MAX_VIDEO_SECONDS = 60

# Upstream media task states mapped onto the public video status vocabulary.
VIDEO_STATUS_MAP = {"processing": "in_progress", "completed": "completed", "failed": "failed"}
PUBLIC_VIDEO_STATUSES = ("queued", "in_progress", "completed", "failed")

_KIND_SOURCES = (
    ("image", IMAGE_TEXT_MODELS), ("edit", IMAGE_EDIT_MODELS), ("video", VIDEO_MODELS))
_STATIC_MEDIA_MODELS = frozenset(model for _, models in _KIND_SOURCES for model in models)


def model_kinds(model) -> tuple:
    """Return the media kinds a statically known model serves; empty for chat models."""
    if not isinstance(model, str):
        return ()
    kinds = []
    for kind, models in _KIND_SOURCES:
        if model in models:
            kinds.append(kind)
    return tuple(kinds)


def catalog_kinds(item) -> tuple:
    """Classify an account-catalog model entry by its tags; only media tags count."""
    tags = item.get("tags") if isinstance(item, dict) else None
    if not isinstance(tags, list):
        return ()
    names = " ".join(str(tag).lower() for tag in tags if isinstance(tag, (str, int)))
    kinds = []
    if "image" in names:
        kinds.extend(("image", "edit"))
    if "video" in names:
        kinds.append("video")
    return tuple(kinds)


def is_media_model(model, catalog_items=()) -> bool:
    """Accept statically known media models plus catalog entries carrying media tags."""
    if model in _STATIC_MEDIA_MODELS:
        return True
    return any(model == item.get("id") and catalog_kinds(item)
               for item in catalog_items if isinstance(item, dict))


def normalize_media_base_url(value):
    """Settings validator: empty, or an http(s) origin with an optional path prefix."""
    value = value.strip()
    if not value:
        return ""
    try:
        parts = urlsplit(value)
        parts.port  # Raises on an invalid port.
    except ValueError:
        raise ValueError("media_public_base_url: 地址无效") from None
    if (parts.scheme not in ("http", "https") or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment):
        raise ValueError("media_public_base_url: 必须以 http:// 或 https:// 开头且不含查询参数")
    return value.rstrip("/")


def _clean_text(value, field, limit=4096) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    if len(value) > limit or any(ord(char) < 32 for char in value):
        raise ValueError(f"{field} 长度或字符无效")
    return value.strip()


def image_request_body(model: str, prompt: str, *, size=None, n=1, quality=None,
                       images=None, input_fidelity=None) -> dict:
    """Shape one /v2/images request mirroring the CLI ImageService payload."""
    body = {"model": model, "prompt": _clean_text(prompt, "prompt")}
    if size is not None:
        body["size"] = _clean_text(size, "size", 64)
    if model in GEMINI_IMAGE_MODELS:
        # The gemini family returns base64 only when asked, like the CLI asks.
        body["response_format"] = "b64_json"
        if n and n > 1:
            body["n"] = n
        if quality is not None:
            body["quality"] = _clean_text(quality, "quality", 64)
    elif n:
        body["n"] = n
    if images is not None:
        if not isinstance(images, (list, tuple)) or not images:
            raise ValueError("images 必须是非空数组")
        body["image"] = [_clean_text(item, "image_url", 262144) for item in images]
        if input_fidelity is not None:
            body["input_fidelity"] = _clean_text(input_fidelity, "input_fidelity", 64)
    return body


def video_request_body(model: str, *, prompt=None, seconds=None, resolution=None,
                       aspect_ratio=None, enable_audio=None, image_url=None) -> dict:
    """Shape one /v2/videos/generations request mirroring the CLI VideoService payload."""
    body = {"model": model, "watermark": True}
    if prompt is not None:
        body["prompt"] = _clean_text(prompt, "prompt")
    if seconds is not None:
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or float(seconds).is_integer() is False:
            raise ValueError("seconds 必须是整数")
        seconds = int(seconds)
        if not 1 <= seconds <= MAX_VIDEO_SECONDS:
            raise ValueError(f"seconds 必须在 1 到 {MAX_VIDEO_SECONDS} 之间")
        body["seconds"] = seconds
    extra = {}
    if resolution is not None:
        if resolution not in VIDEO_RESOLUTIONS:
            raise ValueError(f"resolution 必须是 {' 或 '.join(VIDEO_RESOLUTIONS)}")
        extra["resolution"] = resolution
    if aspect_ratio is not None:
        extra["aspect_ratio"] = _clean_text(aspect_ratio, "aspect_ratio", 32)
    if enable_audio is not None:
        extra["enable_audio"] = bool(enable_audio)
    if extra:
        body["extra_parameters"] = extra
    if image_url is not None:
        body["image_url"] = _clean_text(image_url, "image_url", 262144)
    if "prompt" not in body and "image_url" not in body:
        raise ValueError("prompt 与 image_url 至少提供一个")
    return body


def _data_field(payload: dict):
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else {}


def parse_image_reply(payload) -> tuple[list, float | None]:
    """Return (items, credit) from an image generation or edit reply.

    Accepts the item list and usage nested under ``data`` or at the top level; every
    item keeps whatever of url / b64_json / revised_prompt the upstream supplied.
    """
    if not isinstance(payload, dict):
        raise ValueError("图像响应必须是 JSON 对象")
    data = _data_field(payload)
    items = data.get("data") if isinstance(data.get("data"), list) else payload.get("data")
    if not isinstance(items, list) or not items:
        raise ValueError("图像响应缺少 data 列表")
    clean = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        b64 = item.get("b64_json")
        if (isinstance(url, str) and url) or (isinstance(b64, str) and b64):
            clean.append({key: value for key, value in item.items()
                          if key in ("url", "b64_json", "revised_prompt")})
    if not clean:
        raise ValueError("图像响应缺少可用的图片条目")
    for source in (data.get("usage"), payload.get("usage")):
        if isinstance(source, dict) and isinstance(source.get("credit"), (int, float)):
            return clean, float(source["credit"])
    return clean, None


def parse_video_submit(payload) -> str:
    """Return the upstream task id from a video generation reply."""
    if not isinstance(payload, dict):
        raise ValueError("视频响应必须是 JSON 对象")
    task_id = _data_field(payload).get("id") or payload.get("id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("视频响应缺少任务 ID")
    return task_id.strip()


def map_video_status(status) -> str:
    """Map an upstream task state; unknown non-terminal states stay in progress."""
    if not isinstance(status, str):
        return "in_progress"
    return VIDEO_STATUS_MAP.get(status.strip().lower(), "in_progress")


def parse_video_task(payload) -> tuple[str, list, float | None, str]:
    """Return (public status, resource urls, credit, failure text) from a video task reply."""
    if not isinstance(payload, dict):
        raise ValueError("视频任务响应必须是 JSON 对象")
    data = _data_field(payload)
    status = map_video_status(data.get("status") or payload.get("status"))
    rows = data.get("data") if isinstance(data.get("data"), list) else payload.get("data")
    urls = [item.get("url") for item in rows or []
            if isinstance(item, dict) and isinstance(item.get("url"), str) and item["url"]]
    message = ""
    for source in (data, payload):
        for key in ("error", "message", "msg", "fail_reason"):
            value = source.get(key) if isinstance(source, dict) else None
            if isinstance(value, str) and value.strip():
                message = value.strip()[:500]
                break
        if message:
            break
    for source in (data.get("usage"), payload.get("usage")):
        if isinstance(source, dict) and isinstance(source.get("credit"), (int, float)):
            return status, urls, float(source["credit"]), message
    return status, urls, None, message
