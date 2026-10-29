"""Public reranker contract for the fixed codebase reranking service."""
from __future__ import annotations

import math

MODEL_ID = "reranker"
MAX_DOCUMENTS = 100


class RerankRequestError(ValueError):
    def __init__(self, message: str, param: str):
        super().__init__(message)
        self.param = param


class RerankUpstreamError(Exception):
    def __init__(self, code, message: str):
        super().__init__(message)
        self.upstream_code = code
        self.message = message


def model_details() -> dict:
    return {"id": MODEL_ID, "credits": None, "credits_by_profile": {}, "rerank": True}


def is_rerank_model(model) -> bool:
    return model == MODEL_ID


def request_body(payload) -> tuple[dict, list[str], int, bool]:
    if not isinstance(payload, dict):
        raise RerankRequestError("请求体必须是 JSON 对象", "")
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        raise RerankRequestError("query 必须是非空字符串", "query")
    documents = payload.get("documents")
    if (not isinstance(documents, list) or not 1 <= len(documents) <= MAX_DOCUMENTS
            or any(not isinstance(document, str) for document in documents)):
        raise RerankRequestError(f"documents 必须包含 1–{MAX_DOCUMENTS} 个字符串", "documents")
    top_n = payload.get("top_n", len(documents))
    if type(top_n) is not int or not 1 <= top_n <= len(documents):
        raise RerankRequestError("top_n 必须在 1 到 documents 数量之间", "top_n")
    return_documents = payload.get("return_documents", False)
    if type(return_documents) is not bool:
        raise RerankRequestError("return_documents 必须是布尔值", "return_documents")
    body = {"model": MODEL_ID, "query": query, "documents": documents, "top_n": top_n}
    return body, documents, top_n, return_documents


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


def parse_reply(payload, documents, top_n, return_documents) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("rerank 响应必须是 JSON 对象")
    if payload.get("code") not in (None, 0):
        message = payload.get("msg") or payload.get("message") or "上游 rerank 接口错误"
        raise RerankUpstreamError(payload.get("code"), str(message)[:500])
    output = payload.get("output")
    items = output.get("results") if isinstance(output, dict) else None
    if not isinstance(items, list) or not items:
        raise ValueError("rerank 响应缺少 results")
    results = []
    seen = set()
    for item in items:
        if not isinstance(item, dict) or type(item.get("index")) is not int:
            raise ValueError("rerank 响应包含无效索引")
        index = item["index"]
        if not 0 <= index < len(documents) or index in seen:
            raise ValueError("rerank 响应包含重复或越界索引")
        seen.add(index)
        row = {"index": index}
        if return_documents:
            row["document"] = {"text": documents[index]}
        results.append(row)
    result = {"model": MODEL_ID, "results": results[:top_n]}
    usage = _usage(payload)
    if usage:
        result["usage"] = usage
    return result
