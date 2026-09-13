"""Local model policies layered over account-owned upstream capabilities."""

from contextvars import ContextVar

from fastapi import HTTPException


_request_policy = ContextVar("gateway_model_policy", default=None)


class PolicyScopeMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        token = _request_policy.set({})
        try:
            await self.app(scope, receive, send)
        finally:
            _request_policy.reset(token)


def snapshot(config):
    store = config.get("control_store")
    return store.snapshot() if store is not None else {"models": {}, "credentials": {}, "revision": 0}


def credential_enabled(config, entry):
    return snapshot(config)["credentials"].get(entry.get("account_key"), {}).get("enabled", True)


def default_rule(source):
    return {"public_id": source, "enabled": True, "keep_original": False,
            "region": None, "profile": None, "credential_ids": []}


def rule_for(config, source):
    return {**default_rule(source), **snapshot(config)["models"].get(source, {})}


def _deny(message, code="model_disabled"):
    raise HTTPException(status_code=404, detail={"error": {
        "message": message, "type": "invalid_request_error", "param": "model", "code": code}})


def resolve(config, public_model):
    """Resolve once per request, keeping the public name separate from wire data."""
    if not isinstance(public_model, str) or not public_model.strip():
        raise HTTPException(status_code=400, detail={"error": {"message": "model must be a non-empty string"}})
    state = _request_policy.get()
    if state and state.get("source") == public_model:
        source, rule = state["source"], state["rule"]
        if rule_for(config, source) != rule:
            _deny("模型策略已变更，请重新请求", "model_policy_changed")
        return source
    data = snapshot(config)
    source = next((key for key, value in data["models"].items()
                   if value.get("public_id", key) == public_model), public_model)
    rule = {**default_rule(source), **data["models"].get(source, {})}
    if not rule["enabled"]:
        _deny("模型已停用")
    if source == public_model and rule["public_id"] != source and not rule["keep_original"]:
        _deny("原模型 ID 已停用，请使用已配置的对外 ID", "model_renamed")
    if state is not None:
        state.update(source=source, public_model=public_model, rule=rule)
    return source


def check_resolved(config, source):
    if not rule_for(config, source)["enabled"]:
        _deny("模型已停用")


def route_allowed(config, entry, model, *, rule=None):
    if not credential_enabled(config, entry):
        return False
    active = rule_for(config, model) if model else default_rule("")
    if rule is None:
        state = _request_policy.get()
        if state and state.get("source") == model:
            if state["rule"] != active:
                return False
            rule = state["rule"]
        else:
            rule = active
    if not rule.get("enabled", True):
        return False
    profile = entry.get("profile", "")
    if rule.get("region") and not profile.startswith(rule["region"] + "-"):
        return False
    if rule.get("profile") and profile != rule["profile"]:
        return False
    ids = rule.get("credential_ids") or []
    return not ids or entry.get("account_key") in ids


def sticky_scope(config, key, model):
    if key and config.get("control_store") is not None:
        return f"policy:{snapshot(config)['revision']}:{model}:{key}"
    return key


def public_name(fallback):
    return (_request_policy.get() or {}).get("public_model", fallback)


def public_details(gateway, region=None):
    """Only advertise capabilities and prices from credentials inside the rule."""
    config = gateway.CONFIG
    raw = gateway.current_model_details(region)
    if config.get("control_store") is None:
        return raw
    pool = config.get("cred_pool")
    entries = pool.entries() if pool is not None else []
    out = []
    for item in raw:
        source = item["id"]
        rule = rule_for(config, source)
        if not rule["enabled"]:
            continue
        candidates = [entry for entry in entries if route_allowed(config, entry, source, rule=rule)
                      and pool._eligible(entry, source, region=region)]
        if pool is not None and not candidates:
            continue
        prices = {}
        accounts = config.get("account_catalogs")
        if accounts is not None:
            for entry in candidates:
                profile = entry.get("profile")
                for model in (accounts.get(entry.get("account_key")) or {}).get("models") or []:
                    if model.get("id") == gateway._upstream_model(source, profile):
                        price = gateway._multiplier_value(model.get("credits"))
                        if price is not None:
                            prices[profile] = min(prices.get(profile, price), price)
        else:
            allowed_profiles = {entry.get("profile") for entry in candidates}
            prices = {profile: price for profile, price in item["credits_by_profile"].items()
                      if not pool or profile in allowed_profiles}
        names = [rule["public_id"]]
        if rule["keep_original"] and source not in names:
            names.append(source)
        for name in names:
            out.append({"id": name, "credits": min(prices.values(), default=None),
                        "credits_by_profile": prices})
    return out
