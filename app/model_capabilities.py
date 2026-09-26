"""Publish safe account-owned model declarations and validate explicit request requirements."""
from dataclasses import dataclass
import json
import math
from urllib.parse import urlsplit

from fastapi import HTTPException

from app.safe_logging import sanitize_log_text

from app.model_catalog_view import SharedModel
from app.reasoning import resolve_reasoning_effort, thinking_mode
PROFILES = frozenset(("cn-cli", "cn-work", "intl-cli", "intl-work"))
_TEXT = frozenset(("id", "name", "vendor", "description", "descriptionZh", "descriptionEn", "credits", "summary"))
_BOOL = frozenset(("supportsImages", "disabledMultimodal", "supportsToolCall", "supportsReasoning",
                   "onlyReasoning", "canDisableThinking", "supportsExtra", "isDefault", "disabled"))
_INT = frozenset(("maxInputTokens", "maxOutputTokens", "maxAllowedSize", "top_k"))
_FLOAT = frozenset(("temperature", "top_p", "repetition_penalty"))


def _text(value, limit=4096):
    return sanitize_log_text(value, limit) if isinstance(value, str) else None


def _positive(value):
    return value if type(value) is int and 0 < value <= 2**53 - 1 else None


def sanitize_model(model):
    """Use an explicit schema so future private upstream fields cannot become public by accident."""
    result = {}
    if not isinstance(model, dict):
        return result
    for key in _TEXT:
        if isinstance(model.get(key), str):
            result[key] = _text(model[key])
    for key in _BOOL:
        if type(model.get(key)) is bool:
            result[key] = model[key]
    for key in _INT:
        value = model.get(key)
        if type(value) is int and 0 <= value <= 2**53 - 1:
            result[key] = value
    for key in _FLOAT:
        value = model.get(key)
        if type(value) in (float, int) and abs(value) <= 2**53 - 1 and math.isfinite(value):
            result[key] = value
    tags = model.get("tags")
    if isinstance(tags, list):
        result["tags"] = [_text(tag, 256) for tag in tags[:32] if isinstance(tag, str)]
    icon = model.get("iconUrl")
    if isinstance(icon, str) and len(icon) <= 2048:
        try:
            url = urlsplit(icon)
            if url.scheme == "https" and url.hostname and not (url.username or url.password or url.query or url.fragment):
                result["iconUrl"] = _text(icon, 2048)
        except ValueError:
            pass
    for key, strings, flags, integers, arrays in (
        ("reasoning", ("defaultEffort", "effort", "summary"), ("canDisableThinking",), (), ("supportedEfforts",)),
        ("relatedModels", ("lite", "reasoning"), (), (), ()),
        ("contextWindow", (), (), ("defaultLength",), ("supportedLengths",)),
    ):
        raw = model.get(key)
        if not isinstance(raw, dict):
            continue
        child = {}
        for field in strings:
            if isinstance(raw.get(field), str):
                child[field] = _text(raw[field], 256)
        for field in flags:
            if type(raw.get(field)) is bool:
                child[field] = raw[field]
        for field in integers:
            if _positive(raw.get(field)) is not None:
                child[field] = raw[field]
        for field in arrays:
            values = raw.get(field)
            if isinstance(values, list):
                child[field] = ([n for n in values[:64] if _positive(n) is not None]
                                if field == "supportedLengths" else
                                [_text(n, 64) for n in values[:32] if isinstance(n, str)])
        result[key] = child
    return result


def capabilities(model):
    model = model or {}
    reasoning = model.get("reasoning") if isinstance(model.get("reasoning"), dict) else {}
    images = model.get("supportsImages")
    if model.get("disabledMultimodal") is True:
        images = False
    disable = [value for value in (model.get("canDisableThinking"), reasoning.get("canDisableThinking"))
               if type(value) is bool]
    if not disable and model.get("onlyReasoning") is True:
        disable = [False]
    if model.get("onlyReasoning") is True and True in disable:
        disable.append(False)
    can_disable = disable[0] if disable and len(set(disable)) == 1 else None
    values = {"images": images, "tools": model.get("supportsToolCall"),
              "reasoning": model.get("supportsReasoning"), "thinking_disable": can_disable}
    return {key: value if type(value) is bool else None for key, value in values.items()}


def describe_models(records):
    """Keep distinct per-profile variants without exposing account identifiers."""
    grouped = {}
    declarations = []
    for profile, model in records:
        if profile not in PROFILES:
            continue
        clean = sanitize_model(model)
        if not clean:
            clean = {}  # Missing declarations must not look like unanimous support.
        if clean:
            if isinstance(model, SharedModel):
                clean["catalog_source"] = {"kind": "shared", "profiles": sorted({p for p, _ in model.catalog_sources})}
                origins = []
                for source_profile, raw in model.catalog_sources:
                    source = {"profile": source_profile, "metadata": sanitize_model(raw)}
                    if source not in origins:
                        origins.append(source)
                clean["source_variants"] = sorted(origins, key=lambda item: json.dumps(item, sort_keys=True))
            else:
                clean["catalog_source"] = {"kind": "direct", "profiles": [profile]}
        variants = grouped.setdefault(profile, [])
        if clean not in variants:
            variants.append(clean)
        declarations.append(clean)
    summary = {}
    for key in ("images", "tools", "reasoning", "thinking_disable"):
        values = [capabilities(model)[key] for model in declarations]
        known = {value for value in values if value is not None}
        summary[key] = ("mixed" if len(known) > 1 else "unknown" if not values or None in values else
                        "supported" if values[0] else "unsupported")
    limits = {}
    for key in ("maxInputTokens", "maxOutputTokens"):
        values = [_positive(model.get(key)) for model in declarations]
        known = {value for value in values if value is not None}
        state = "mixed" if len(known) > 1 else "unknown" if not values or None in values else "known"
        limits[key] = {"state": state, "value": values[0] if state == "known" else None}
    return {"capabilities": summary, "limits": limits,
            "metadata_by_profile": {profile: sorted(variants, key=lambda m: json.dumps(m, sort_keys=True))
                                    for profile, variants in sorted(grouped.items())}}


def entry_model(gateway, entry, name):
    profile = entry.get("profile")
    accounts = gateway.CONFIG.get("account_catalogs")
    if accounts is not None or gateway.CONFIG.get("model_cache") is not None:
        account = (accounts or {}).get(entry.get("account_key")) or {}
        models = gateway._effective_account_scope(
            account, "serves", model_id=gateway._upstream_model(name, profile)) if account.get("profile") == profile else []
    else:
        models = gateway._models_for_profile(profile, scope="serves", model_id=gateway._upstream_model(name, profile))
    upstream = gateway._upstream_model(name, profile)
    return next((model for model in models or [] if model.get("id") == upstream), None)


def declaration_prices(gateway, metadata):
    prices = {}
    for profile, variants in metadata["metadata_by_profile"].items():
        values = [value for model in variants
                  if (value := gateway._multiplier_value(model.get("credits"))) is not None and math.isfinite(value)]
        if values:
            prices[profile] = min(values)
    return {"credits": min(prices.values(), default=None), "credits_by_profile": prices}



def route_metadata(gateway, name, *, entries=None, rule=None, region=None, include_disabled=False):
    pool = gateway.CONFIG.get("cred_pool")
    if entries is None:
        entries = pool.entries() if pool is not None else []
    records = []
    for entry in entries:
        profile = entry.get("profile")
        if profile not in PROFILES or not gateway._in_region(profile, region):
            continue
        if include_disabled:
            scope = rule or {}
            if (scope.get("region") and not profile.startswith(scope["region"] + "-")) or (
                    scope.get("profile") and profile != scope["profile"]) or (
                    scope.get("credential_ids") and entry.get("account_key") not in scope["credential_ids"]):
                continue
        elif not gateway.model_policy.route_allowed(gateway.CONFIG, entry, name, rule=rule):
            continue
        model = entry_model(gateway, entry, name)
        if model is not None or name == "auto":
            records.append((profile, model))
    return describe_models(records)


@dataclass(frozen=True)
class Requirements:
    images: bool = False
    tools: bool = False
    effort: str | None = None
    thinking: str | None = None
    max_output: int | None = None
    output_param: str = "max_tokens"
    image_param: str = "messages"

    @classmethod
    def from_request(cls, body, payload=None, protocol="chat"):
        messages = body.get("messages") or []
        images = any(isinstance(message, dict) and isinstance(message.get("content"), list)
                     and any(isinstance(part, dict) and part.get("type") == "image_url" for part in message["content"])
                     for message in messages)
        tools = bool(body.get("tools")) or any(isinstance(message, dict) and
                    (message.get("role") == "tool" or message.get("tool_calls")) for message in messages)
        effort = body.get("reasoning_effort")
        thinking = thinking_mode(payload or {}) if protocol == "messages" else None
        output = body.get("max_tokens")
        param = "max_output_tokens" if protocol == "responses" else "max_tokens"
        if output is not None and _positive(output) is None:
            raise capability_error([("invalid_output_limit", param, "输出上限必须为正整数")])
        if effort is not None and (not isinstance(effort, str) or not effort):
            raise capability_error([("invalid_reasoning_effort", "reasoning_effort", "思考强度必须为非空字符串")])
        return cls(images, tools, effort, thinking, output, param, "input" if protocol == "responses" else "messages")

    def violations(self, model):
        model = model or {}
        caps = capabilities(model)
        failures = []
        if self.images and caps["images"] is False:
            failures.append(("unsupported_image_input", self.image_param, "当前路由的模型声明不支持图片输入"))
        if self.tools and caps["tools"] is False:
            failures.append(("unsupported_tools", "tools", "当前路由的模型声明不支持工具调用"))
        effort = resolve_reasoning_effort(self.effort, self.thinking, model)
        enabled = effort not in (None, "none")
        disabled = effort == "none"
        if enabled and caps["reasoning"] is False:
            failures.append(("unsupported_reasoning", "reasoning_effort", "当前路由的模型声明不支持思考"))
        if disabled and caps["thinking_disable"] is False:
            failures.append(("reasoning_required", "reasoning_effort", "当前路由的模型声明不能关闭思考"))
        reasoning = model.get("reasoning") if isinstance(model.get("reasoning"), dict) else {}
        efforts = reasoning.get("supportedEfforts")
        if (effort not in (None, "none") and isinstance(efforts, list) and (efforts or isinstance(model, SharedModel))
                and all(isinstance(value, str) for value in efforts) and effort not in efforts):
            failures.append(("unsupported_reasoning_effort", "reasoning_effort", "思考强度不在当前模型声明的选项中"))
        maximum = _positive(model.get("maxOutputTokens"))
        if maximum is not None and self.max_output is not None and self.max_output > maximum:
            failures.append(("model_output_limit", self.output_param, f"请求的输出上限超过当前模型声明的 {maximum} token"))
        return failures


def capability_error(failures):
    unique = list(dict.fromkeys(failures))[:8]
    if not unique:
        return HTTPException(status_code=503, headers={"Retry-After": "1"}, detail={"error": {
            "type": "service_unavailable", "code": "model_capability_not_ready",
            "message": "模型目录正在变化，请稍后重试",
        }})
    code = unique[0][0] if len(unique) == 1 else "model_capability_mismatch"
    return HTTPException(status_code=400, detail={"error": {
        "type": "invalid_request_error", "code": code, "param": unique[0][1],
        "message": "；".join(item[2] for item in unique),
    }})
