"""Local model policies layered over account-owned upstream capabilities."""
from contextvars import ContextVar
import threading

from fastapi import HTTPException

from app import growth_tasks, perf_metrics, travel, embeddings
from app.model_capabilities import route_metadata
from app.api_keys import api_principal
_request_policy = ContextVar("gateway_model_policy", default=None)
_route_counter_lock = threading.Lock()
_route_counter = 0


def _principal():
    return api_principal.get()


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
    if source in (embeddings.MODEL_ID, embeddings.PUBLIC_MODEL_ID):
        return {"public_id": embeddings.PUBLIC_MODEL_ID, "upstream_id": embeddings.MODEL_ID,
                "custom": False, "enabled": True, "keep_original": False,
                "region": None, "profile": None, "credential_ids": []}
    return {"public_id": source, "upstream_id": source, "custom": False,
            "enabled": True, "keep_original": False,
            "region": None, "profile": None, "credential_ids": []}

def rule_for(config, source, policy=None):
    return {**default_rule(source), **_policy(config, policy)["models"].get(source, {})}


def _route_candidates_for_rule(rule: dict, auto_routes=None) -> list[str]:
    routes = rule.get("routes")
    if not isinstance(routes, list) and isinstance(auto_routes, list):
        routes = auto_routes
    if not isinstance(routes, list):
        return [rule["upstream_id"]]
    enabled = [route for route in routes if route.get("enabled", True)]
    if not enabled:
        return []
    weighted = any(route.get("weight") is not None for route in enabled)
    if not weighted:
        return [route["upstream_id"] for route in enabled]
    weights = [route.get("weight") if route.get("weight") is not None else 1.0 for route in enabled]
    total = sum(weights)
    if total <= 0:
        return []
    global _route_counter
    with _route_counter_lock:
        offset = (_route_counter * 1000) % max(1, int(round(total * 1000)))
        _route_counter += 1
    cursor = 0
    selected = 0
    for index, weight in enumerate(weights):
        cursor += int(round(weight * 1000))
        if offset < cursor:
            selected = index
            break
    return [enabled[selected]["upstream_id"]] + [route["upstream_id"] for i, route in enumerate(enabled) if i != selected]


def route_candidates(config, public_model: str, policy=None) -> list[str]:
    """Return enabled upstream candidates, preserving order or applying route weights."""
    state = _request_policy.get()
    if state and public_model in (state.get("public_model"), state.get("upstream")):
        return list(state.get("route_candidates") or ())
    source = source_for(config, public_model)
    return _route_candidates_for_rule(rule_for(config, source, policy),
                                      (config.get("model_route_groups") or {}).get(source))


def route_ids(config, public_model: str, policy=None) -> list[str]:
    """Return all enabled route IDs without applying weighted selection."""
    source = source_for(config, public_model)
    rule = rule_for(config, source, policy)
    routes = rule.get("routes")
    if not isinstance(routes, list):
        routes = (config.get("model_route_groups") or {}).get(source)
    if not isinstance(routes, list):
        return [rule["upstream_id"]]
    return [route["upstream_id"] for route in routes if route.get("enabled", True)]


def bind_route(config, upstream: str, index: int | None = None) -> None:
    """Bind a retry attempt to one upstream while retaining the aggregate policy rule."""
    state = _request_policy.get()
    if state is not None:
        state["upstream"] = upstream
        if index is not None:
            state["route_index"] = index


def source_for(config, name):
    """Resolve a public model name or automatic alias to its policy source."""
    if name == "default-model":
        return "auto"
    if name in (embeddings.MODEL_ID, embeddings.PUBLIC_MODEL_ID):
        return embeddings.MODEL_ID
    aliases = config.get("model_route_aliases") or {}
    if name in aliases and name not in snapshot(config).get("models", {}):
        return aliases[name]
    data = snapshot(config)
    for source, rule in data.get("models", {}).items():
        if rule.get("public_id", source) == name:
            return source
        if rule.get("keep_original") and source == name:
            return source
    return name


def _restriction_source(source):
    """Use the canonical automatic-model source for key restrictions."""
    return "auto" if source == "default-model" else source

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
    principal = _principal()
    if state and public_model in (state.get("upstream"), state.get("public_model")):
        if principal is not None and principal.models is not None and _restriction_source(state.get("source")) not in principal.models:
            raise HTTPException(status_code=403, detail={"error": {
                "message": "模型不在当前 API key 的允许列表中", "type": "permission_error",
                "code": "model_not_allowed", "param": "model"}})
        if rule_for(config, state["source"]) != state["rule"]:
            _deny("模型策略已变更，请重新请求", "model_policy_changed")
        return state["upstream"]
    data = snapshot(config)
    source = next((key for key, value in data["models"].items()
                   if value.get("public_id", key) == public_model),
                  embeddings.MODEL_ID if public_model in (embeddings.MODEL_ID, embeddings.PUBLIC_MODEL_ID)
                  else public_model)
    rule = {**default_rule(source), **data["models"].get(source, {})}
    if principal is not None and principal.models is not None and _restriction_source(source) not in principal.models:
        raise HTTPException(status_code=403, detail={"error": {
            "message": "模型不在当前 API key 的允许列表中", "type": "permission_error",
            "code": "model_not_allowed", "param": "model"}})
    if not rule["enabled"]:
        _deny("模型已停用")
    if source == public_model and (rule["custom"] or (rule["public_id"] != source and not rule["keep_original"])):
        _deny("原模型 ID 已停用，请使用已配置的对外 ID", "model_renamed")
    candidates = _route_candidates_for_rule(rule, (config.get("model_route_groups") or {}).get(source))
    if state is None and (not candidates or candidates[0] != source) and rule["upstream_id"] != source:
        raise HTTPException(status_code=503, detail={"error": {"message": "模型路由上下文未初始化",
                            "type": "server_error", "code": "model_policy_context_missing"}})
    if not candidates:
        _deny("模型路由已全部停用")
    if state is not None:
        state.update(source=source, public_model=public_model, upstream=candidates[0],
                     route_candidates=candidates, route_index=0, rule=rule)
    return candidates[0]


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
    """Publish chat, media and embedding routes using their own eligibility checks."""
    config = gateway.CONFIG
    known = {item["id"]: item for item in gateway.current_model_details(region)}
    for media in gateway.media_model_details():
        row = known.get(media["id"])
        known[media["id"]] = {**media, **(row or {})}
    for embedding in gateway.embedding_model_details():
        row = known.get(embedding["id"])
        known[embedding["id"]] = {**embedding, **(row or {})}
    for reranker in gateway.rerank_model_details():
        known[reranker["id"]] = {**reranker, **(known.get(reranker["id"]) or {})}
    policy = view(config)
    policies = policy.data["models"]
    owners = {rule.get("public_id", source): source for source, rule in policies.items()}
    pool = config.get("cred_pool")
    entries = pool.entries() if pool is not None else []
    out = []
    for source in dict.fromkeys([*known, *policies]):
        if source in (config.get("model_route_aliases") or {}) and source not in policies:
            continue
        rule = rule_for(config, source, policy)
        upstream = rule["upstream_id"]
        route_names = route_ids(config, source, policy)
        item = next((known.get(route) for route in route_names if known.get(route) is not None), None)
        if not rule["enabled"] or item is None:
            continue
        if not route_names:
            continue
        media = bool(item.get("media_kinds"))
        embedding = bool(item.get("embedding"))
        reranking = bool(item.get("rerank"))
        token = _request_policy.set({"source": source, "upstream": upstream,
                                     "public_model": rule["public_id"], "rule": rule})
        try:
            if media:
                candidates = [entry for entry in entries if gateway._media_account_reason(
                    entry, upstream, rule=rule, policy=policy, region=region) is None]
                if pool is None:
                    cm = config.get("cred")
                    candidates = []
                    if cm is not None:
                        with cm._lock:
                            headers = cm.get_headers()
                        profile = gateway.profile_for_headers(headers)
                        identity = gateway.account_key(profile, headers.get("X-User-Id"),
                                                       headers.get("X-Enterprise-Id"))
                        entry = {"profile": profile, "account_key": identity}
                        if (profile in gateway.media_generation.MEDIA_PROFILES
                                and route_allowed(config, entry, upstream, rule=rule, policy=policy)):
                            candidates = [entry]
            elif embedding or reranking:
                candidates = [entry for entry in entries if gateway._codebase_account_reason(
                    entry, upstream, rule=rule, policy=policy, region=region) is None]
                if pool is None:
                    cm = config.get("cred")
                    candidates = []
                    if cm is not None:
                        with cm._lock:
                            headers = cm.get_headers()
                        profile = gateway.profile_for_headers(headers)
                        identity = gateway.account_key(profile, headers.get("X-User-Id"),
                                                       headers.get("X-Enterprise-Id"))
                        entry = {"profile": profile, "account_key": identity}
                        if (profile in gateway.embeddings.PROFILES
                                and route_allowed(config, entry, upstream, rule=rule, policy=policy)):
                            candidates = [entry]
            else:
                candidates = [entry for entry in entries if route_allowed(
                    config, entry, upstream, rule=rule, policy=policy)
                    and pool._eligible(entry, upstream, region=region)]
        finally:
            _request_policy.reset(token)
        if not candidates and (pool is not None or media or embedding or reranking):
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
            if source in (config.get("model_route_groups") or {}) and "routes" not in rule:
                row["routes"] = config["model_route_groups"][source]
            for key in ("media_kinds", "media_sources", "media_defaults", "embedding", "rerank"):
                if key in item:
                    row[key] = item[key]
            if any(prices[profile] == best for profile in measured):
                row["credits_estimated"] = True   # Upstream declares it free but charges it.
            out.append(row)
    return out
