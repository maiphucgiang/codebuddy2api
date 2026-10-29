"""OpenAI-compatible codebase embedding request and response contracts."""
from __future__ import annotations

import base64
import binascii
import math
import struct

from app.site_routing import EMBEDDING_ENDPOINTS

MODEL_ID = "bge-m3"
PUBLIC_MODEL_ID = "baai/bge-m3"
QWEN_MODEL_ID = "Qwen/Qwen3-Embedding-0.6B"
PROFILES = frozenset(EMBEDDING_ENDPOINTS)
DIMENSIONS = 1024
MAX_INPUT_ITEMS = 2048


class EmbeddingRequestError(ValueError):
    def __init__(self, message: str, param: str):
        super().__init__(message)
        self.param = param


class EmbeddingUpstreamError(Exception):
    def __init__(self, code, message: str):
        super().__init__(message)
        self.upstream_code = code
        self.message = message


def model_details() -> list:
    return [
        {"id": MODEL_ID, "credits": None, "credits_by_profile": {}, "embedding": True},
        {"id": QWEN_MODEL_ID, "credits": None, "credits_by_profile": {}, "embedding": True},
    ]


def is_embedding_model(model) -> bool:
    return model in (MODEL_ID, QWEN_MODEL_ID)


def codebase_model(model: str) -> str:
    if model == MODEL_ID:
        return "model2"
    if model == QWEN_MODEL_ID:
        return model
    raise ValueError("不支持的 codebase embedding 模型")


def _token_ids(values) -> list[int]:
    if not values or any(type(value) is not int or not 0 <= value <= 2**31 - 1 for value in values):
        raise EmbeddingRequestError("token ID 必须是非空整数数组", "input")
    return values


def _input(value) -> int:
    if isinstance(value, str):
        if not value:
            raise EmbeddingRequestError("input 不能为空字符串", "input")
        return 1
    if not isinstance(value, list) or not value:
        raise EmbeddingRequestError("input 必须是字符串、字符串数组或 token ID 数组", "input")
    if all(isinstance(item, str) for item in value):
        if len(value) > MAX_INPUT_ITEMS or any(not item for item in value):
            raise EmbeddingRequestError(f"字符串数组须包含 1–{MAX_INPUT_ITEMS} 个非空字符串", "input")
        return len(value)
    if all(type(item) is int for item in value):
        _token_ids(value)
        return 1
    if all(isinstance(item, list) for item in value):
        if len(value) > MAX_INPUT_ITEMS:
            raise EmbeddingRequestError(f"token 数组最多 {MAX_INPUT_ITEMS} 项", "input")
        for item in value:
            _token_ids(item)
        return len(value)
    raise EmbeddingRequestError("input 数组的元素类型必须一致", "input")


def request_body(payload, model: str) -> tuple[dict, str, int]:
    """Validate public input and pin the upstream representation to float vectors."""
    if not isinstance(payload, dict):
        raise EmbeddingRequestError("请求体必须是 JSON 对象", "")
    encoding = payload.get("encoding_format", "float")
    if encoding not in ("float", "base64"):
        raise EmbeddingRequestError("encoding_format 必须是 float 或 base64", "encoding_format")
    dimensions = payload.get("dimensions")
    if dimensions is not None and (type(dimensions) is not int or dimensions != DIMENSIONS):
        raise EmbeddingRequestError(f"当前向量模型的 dimensions 仅支持 {DIMENSIONS}", "dimensions")
    user = payload.get("user")
    if user is not None and (not isinstance(user, str) or len(user) > 512):
        raise EmbeddingRequestError("user 必须是最多 512 字符的字符串", "user")
    count = _input(payload.get("input"))
    return {"model": model, "input": payload["input"], "encoding_format": "float"}, encoding, count


def _numbers(value) -> list[float]:
    if not isinstance(value, list) or len(value) != DIMENSIONS:
        raise ValueError("embedding 响应的向量维度无效")
    if any(type(number) not in (int, float) or not math.isfinite(number) for number in value):
        raise ValueError("embedding 响应包含无效向量数值")
    return value


def _vector(value) -> list[float]:
    if isinstance(value, list):
        return _numbers(value)
    if not isinstance(value, str):
        raise ValueError("embedding 响应缺少向量")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("embedding 响应的 base64 向量无效") from None
    if len(raw) != DIMENSIONS * 4:
        raise ValueError("embedding 响应的向量维度无效")
    try:
        return list(struct.unpack(f"<{DIMENSIONS}f", raw))
    except struct.error:
        raise ValueError("embedding 响应的 base64 向量无效") from None


def _base64(values) -> str:
    try:
        return base64.b64encode(struct.pack(f"<{len(values)}f", *values)).decode("ascii")
    except (struct.error, OverflowError):
        raise ValueError("embedding 向量超出 float32 范围") from None


def _usage(payload) -> dict:
    source = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    result = {}
    for key in ("prompt_tokens", "total_tokens", "credit"):
        value = source.get(key)
        if key == "prompt_tokens" and value is None:
            value = source.get("input_tokens")
        if type(value) in (int, float) and math.isfinite(value) and value >= 0:
            result[key] = value
    return result


def parse_reply(payload, model: str, encoding: str, expected_count: int) -> dict:
    """Validate every vector and restore OpenAI ordering and encoding."""
    if not isinstance(payload, dict):
        raise ValueError("embedding 响应必须是 JSON 对象")
    if payload.get("code") not in (None, 0):
        message = payload.get("msg") or payload.get("message") or "上游 embedding 接口错误"
        raise EmbeddingUpstreamError(payload.get("code"), str(message)[:500])
    items = payload.get("data")
    if not isinstance(items, list) or len(items) != expected_count:
        raise ValueError("embedding 响应的条目数量与请求不一致")
    data = []
    for position, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError("embedding 响应包含无效条目")
        index = item.get("index", position)
        if type(index) is not int:
            raise ValueError("embedding 响应的索引无效")
        values = _vector(item.get("embedding"))
        data.append((index, {"object": "embedding", "index": index,
                             "embedding": _base64(values) if encoding == "base64" else values}))
    if sorted(index for index, _item in data) != list(range(expected_count)):
        raise ValueError("embedding 响应的索引不完整")
    result = {"object": "list", "data": [item for _index, item in sorted(data, key=lambda row: row[0])],
              "model": model}
    usage = _usage(payload)
    if usage:
        result["usage"] = usage
    return result
