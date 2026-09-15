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


def credential_auto_checkin(config, entry):
    """International accounts opt in explicitly; the preference is independent of routing."""
    default = entry.get("profile") in {"cn-cli", "cn-work"}
    return snapshot(config)["credentials"].get(entry.get("account_key"), {}).get("auto_checkin", default)


def credential_auto_travel(config, entry):
    supported = entry.get("profile") in {"cn-cli", "cn-work"}
    return supported and snapshot(config)["credentials"].get(entry.get("account_key"), {}).get("auto_travel", True)


def default_rule(source):
    return {"public_id": source, "upstream_id": source, "custom": False,
            "enabled": True, "keep_original": False,
            "region": None, "profile": None, "credential_ids": []}


def rule_for(config, source):
    return {**default_rule(source), **snapshot(config)["models"].get(source, {})}


def _deny(message, code="model_disabled"):
    raise HTTPException(status_code=404, detail={"error": {
        "message": message, "type": "invalid_request_error", "param": "model", "code": code}})


def _active(config, upstream):
    state = _request_policy.get()
    if state and state.get("upstream") == upstream:
        return rule_for(config, state["source"]), state
    return rule_for(config, upstream), None


def resolve(config, public_model):
    """Resolve one public name to a literal upstream ID while retaining its own policy."""
    if not isinstance(public_model, str) or not public_model.strip():
        raise HTTPException(status_code=400, detail={"error": {"message": "model must be a non-empty string"}})
    state = _request_policy.get()
    if state and public_model in (state.get("upstream"), state.get("public_model")):
        if rule_for(config, state["source"]) != state["rule"]:
            _deny("模型策略已变更，请重新请求", "model_policy_changed")
        return state["upstream"]
    data = snapshot(config)
    source = next((key for key, value in data["models"].items()
                   if value.get("public_id", key) == public_model), public_model)
    rule = {**default_rule(source), **data["models"].get(source, {})}
    if not rule["enabled"]:
        _deny("模型已停用")
    if source == public_model and (rule["custom"] or (rule["public_id"] != source and not rule["keep_original"])):
        _deny("原模型 ID 已停用，请使用已配置的对外 ID", "model_renamed")
    if state is None and rule["upstream_id"] != source:
        raise HTTPException(status_code=503, detail={"error": {"message": "模型路由上下文未初始化",
                            "type": "server_error", "code": "model_policy_context_missing"}})
    if state is not None:
        state.update(source=source, public_model=public_model, upstream=rule["upstream_id"], rule=rule)
    return rule["upstream_id"]


def check_resolved(config, upstream):
    rule, state = _active(config, upstream)
    if state and state["rule"] != rule:
        _deny("模型策略已变更，请重新请求", "model_policy_changed")
    if not rule["enabled"]:
        _deny("模型已停用")


def route_allowed(config, entry, model, *, rule=None):
    if not credential_enabled(config, entry):
        return False
    if rule is None:
        active, state = _active(config, model) if model else (default_rule(""), None)
        if state and state["rule"] != active:
            return False
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
        state = _request_policy.get() or {}
        return f"policy:{snapshot(config)['revision']}:{state.get('source', model)}:{key}"
    return key


def public_name(fallback):
    return (_request_policy.get() or {}).get("public_model", fallback)


def public_details(gateway, region=None):
    """Publish each route using only its upstream capabilities and allowed accounts."""
    config = gateway.CONFIG
    raw = gateway.current_model_details(region)
    if config.get("control_store") is None:
        return raw
    known = {item["id"]: item for item in raw}
    policies = snapshot(config)["models"]
    owners = {rule.get("public_id", source): source for source, rule in policies.items()}
    pool = config.get("cred_pool")
    entries = pool.entries() if pool is not None else []
    out = []
    for source in dict.fromkeys([*known, *policies]):
        rule = rule_for(config, source)
        upstream = rule["upstream_id"]
        item = known.get(upstream)
        if not rule["enabled"] or item is None:
            continue
        token = _request_policy.set({"source": source, "upstream": upstream,
                                     "public_model": rule["public_id"], "rule": rule})
        try:
            candidates = [entry for entry in entries if route_allowed(config, entry, upstream, rule=rule)
                          and pool._eligible(entry, upstream, region=region)]
        finally:
            _request_policy.reset(token)
        if pool is not None and not candidates:
            continue
        prices = {}
        accounts = config.get("account_catalogs")
        if accounts is not None:
            for entry in candidates:
                profile = entry.get("profile")
                for model in gateway._account_scope(accounts.get(entry.get("account_key")) or {}, "serves") or []:
                    if model.get("id") == gateway._upstream_model(upstream, profile):
                        price = gateway._multiplier_value(model.get("credits"))
                        if price is not None:
                            prices[profile] = min(prices.get(profile, price), price)
        else:
            allowed_profiles = {entry.get("profile") for entry in candidates}
            prices = {profile: price for profile, price in item["credits_by_profile"].items()
                      if not pool or profile in allowed_profiles}
        names = [rule["public_id"]]
        if rule["keep_original"] and not rule["custom"] and source not in names:
            names.append(source)
        for name in names:
            # A later catalog refresh must not overwrite an explicitly configured alias.
            if name in owners and owners[name] != source:
                continue
            out.append({"id": name, "credits": min(prices.values(), default=None),
                        "credits_by_profile": prices})
    return out
