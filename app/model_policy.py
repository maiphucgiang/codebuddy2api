"""Local model policies layered over account-owned upstream capabilities."""
from contextvars import ContextVar

from fastapi import HTTPException

from app import growth_tasks, perf_metrics, travel
from app.model_capabilities import route_metadata
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
    """Return a detached policy snapshot; one routing decision resolves it exactly once."""
    store = config.get("control_store")
    if store is None:
        return {"models": {}, "credentials": {}, "revision": 0}
    perf_metrics.inc("policy_snapshot_calls")
    return store.snapshot()


class PolicyView:
    """One published policy revision reused across a whole routing decision."""

    __slots__ = ("data",)

    def __init__(self, data):
        self.data = data

    @property
    def revision(self):
        return self.data.get("revision", 0)

    def credential(self, entry):
        """Return the account metadata block for this entry, or an empty mapping."""
        return (self.data.get("credentials") or {}).get(entry.get("account_key")) or {}

    def rule_for(self, source):
        return {**default_rule(source), **(self.data.get("models") or {}).get(source, {})}


def view(config):
    """Resolve the policy once so every candidate check in one request sees the same revision."""
    return PolicyView(snapshot(config))


def revision(config, policy=None):
    """Return the published revision without copying the snapshot."""
    if policy is not None:
        return policy.revision
    store = config.get("control_store")
    if store is None:
        return 0
    value = getattr(store, "revision", None)
    return value if isinstance(value, int) else snapshot(config).get("revision", 0)


def _policy(config, policy=None):
    return policy.data if policy is not None else snapshot(config)


def credential_enabled(config, entry, policy=None):
    return _policy(config, policy)["credentials"].get(entry.get("account_key"), {}).get("enabled", True)



def credential_auto_checkin(config, entry, policy=None):
    """International accounts opt in explicitly; the preference is independent of routing."""
    default = entry.get("profile") in {"cn-cli", "cn-work"}
    return _policy(config, policy)["credentials"].get(entry.get("account_key"), {}).get("auto_checkin", default)


def credential_auto_travel(config, entry, policy=None):
    # The growth center is personal-only, so an enterprise tenant must never be scheduled
    # for travel even though it shares the domestic product profile.
    supported = travel.supported(entry.get("profile"), entry.get("enterprise") is True)
    return supported and _policy(config, policy)["credentials"].get(entry.get("account_key"), {}).get("auto_travel", True)


def credential_auto_daily_chat(config, entry, policy=None):
    """A turn spends credits on the account, so international accounts opt in explicitly."""
    supported = entry.get("profile") == "intl-work"
    return supported and _policy(config, policy)["credentials"].get(entry.get("account_key"), {}).get("auto_daily_chat", False)


def credential_auto_growth(config, entry, policy=None):
    """Growth-center sweep is pure accept/claim, so it defaults on for eligible accounts."""
    return growth_tasks.supported(entry.get("profile"), entry.get("enterprise") is True) and \
        _policy(config, policy)["credentials"].get(entry.get("account_key"), {}).get("auto_growth", True)


def growth_auto_tasks(config, entry, policy=None):
    """Task codes whose automation chains the sweep may drive for this account."""
    if not credential_auto_growth(config, entry, policy):
        return frozenset()
    mapping = _policy(config, policy)["credentials"].get(entry.get("account_key"), {}).get("growth_auto") or {}
    return frozenset(code for code, enabled in mapping.items() if enabled is True)


def default_rule(source):
    return {"public_id": source, "upstream_id": source, "custom": False,
            "enabled": True, "keep_original": False,
            "region": None, "profile": None, "credential_ids": []}


def rule_for(config, source, policy=None):
    return {**default_rule(source), **_policy(config, policy)["models"].get(source, {})}


def _deny(message, code="model_disabled"):
    raise HTTPException(status_code=404, detail={"error": {
        "message": message, "type": "invalid_request_error", "param": "model", "code": code}})


def _active(config, upstream, policy=None):
    state = _request_policy.get()
    if state and state.get("upstream") == upstream:
        return rule_for(config, state["source"], policy), state
    return rule_for(config, upstream, policy), None


def active_rule(config, model, policy=None):
    """Return the rule governing this request's model, including a request-scoped alias."""
    if not model:
        return default_rule("")
    return _active(config, model, policy)[0]


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


def route_allowed(config, entry, model, *, rule=None, policy=None):
    if not credential_enabled(config, entry, policy):
        return False
    if rule is None:
        active, state = _active(config, model, policy) if model else (default_rule(""), None)
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
        prices, measured = {}, set()
        accounts = config.get("account_catalogs")
        if accounts is not None:
            for entry in candidates:
                profile = entry.get("profile")
                for model in gateway._published_account_scope(
                        accounts.get(entry.get("account_key")) or {}, "serves", model_id=gateway._upstream_model(upstream, profile)) or []:
                    if model.get("id") == gateway._upstream_model(upstream, profile):
                        price = gateway._multiplier_value(model.get("credits"))
                        if price is not None and price <= prices.get(profile, price):
                            prices[profile] = price
                            if model.get("credits_source") == "measured":
                                measured.add(profile)
                            else:
                                measured.discard(profile)
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
            best = min(prices.values(), default=None)
            row = {"id": name, "credits": best, "credits_by_profile": prices,
                   **route_metadata(gateway, upstream, entries=candidates, rule=rule, region=region)}
            if any(prices[profile] == best for profile in measured):
                row["credits_estimated"] = True   # Upstream declares it free but charges it.
            out.append(row)
    return out
