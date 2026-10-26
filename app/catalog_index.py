"""Read-only indexes over published account catalogs, built once per catalog revision."""
from __future__ import annotations
from .media_generation import catalog_kinds
from .model_catalog_view import INTERNATIONAL, share_models

def account_scope(account, scope="models"):
    """Merge root candidates into selector models while retaining selector metadata."""
    picker = account.get("models")
    if scope == "models" or picker is None:
        return picker
    seen = {item.get("id") for item in picker}
    # Root entries supply missing names without replacing selector metadata.
    return picker + [item for item in account.get("serves") or [] if item.get("id") not in seen]


class CatalogIndex:
    """Precompute per-account and per-profile catalogs so request paths never rescan accounts.

    ``accounts`` is the published ``account_catalogs`` mapping; the index keeps that same
    object so a caller can detect a republished catalog by identity.
    """

    SCOPES = ("models", "serves")

    def __init__(self, accounts, *, revision=0, policy_revision=0, is_enabled=None, scope_fn=None):
        self.accounts = accounts if isinstance(accounts, dict) else {}
        # The gateway passes a scope reader that applies its effective model rates.
        self._scope = scope_fn or account_scope
        self.revision = revision
        self.policy_revision = policy_revision
        self._enabled = is_enabled or (lambda _identity: True)
        self._account: dict[tuple[int, str], list[dict] | None] = {}
        self.media_kinds: dict[str, tuple] = {}  # model id → media kinds from serves tags.
        self._profile: dict[str, dict[str, tuple[dict, ...]]] = {}
        self.profiles: tuple[str, ...] = tuple(dict.fromkeys(
            account.get("profile") for account in self.accounts.values() if account.get("profile")))
        for scope in self.SCOPES:
            sources = self._shared_sources(scope)
            merged: dict[str, list[dict]] = {}
            for identity, account in self.accounts.items():
                profile = account.get("profile") if isinstance(account, dict) else None
                if not profile:
                    continue
                items = self._effective(account, scope, sources)
                if items is None:
                    continue
                if scope == "serves":
                    # Media membership comes from the root table; keep it O(1) per model.
                    for item in items:
                        kinds = catalog_kinds(item)
                        if kinds and isinstance(item.get("id"), str):
                            known = self.media_kinds.setdefault(item["id"], ())
                            self.media_kinds[item["id"]] = tuple(dict.fromkeys(known + kinds))
                merged.setdefault(profile, []).extend(items)
            self._profile[scope] = {name: tuple(items) for name, items in merged.items()}

    def _shared_sources(self, scope):
        """Collect every enabled international account catalog once per scope."""
        return [(account["profile"], self._scope(account, scope))
                for identity, account in self.accounts.items()
                if isinstance(account, dict) and account.get("profile") in INTERNATIONAL
                and account.get("models") is not None and self._enabled(identity)]

    def _effective(self, account, scope, sources):
        native = self._scope(account, scope)
        if native is None:
            return None
        profile = account.get("profile")
        items = share_models(native, profile, sources) if profile in INTERNATIONAL else native
        self._account[(id(account), scope)] = items
        return items

    def account_catalog(self, account, scope="models"):
        """Return this account's effective catalog, or None when the index never saw it."""
        if not isinstance(account, dict):
            return None
        return self._account.get((id(account), scope))

    def catalog(self, profile, scope="models", *, model_id=None):
        """Return the merged catalog for one profile, or None when no account supplied one."""
        items = self._profile.get(scope, {}).get(profile)
        if items is None:
            return None
        if model_id is None:
            return list(items)
        return [item for item in items if item.get("id") == model_id]
