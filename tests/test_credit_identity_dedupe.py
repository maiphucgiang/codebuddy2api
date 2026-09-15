#!/usr/bin/env python3
"""Count each account balance once across duplicate credential paths."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Allow direct execution.

from app import credits
from app.credits import CreditLedger, aggregate_credits, dedupe_by_identity

IDENTITY = "a" * 64
OTHER = "b" * 64


def _balance(remaining, total=None, fetched_at=0.0, intl=False):
    return {"credits": float(remaining), "count": 1, "intl": intl,
            "fetched_at": fetched_at, "soonest_expiry": None,
            "segments": [{"remaining": float(remaining),
                          "total": float(total if total is not None else remaining),
                          "expires_at": None}]}


def _snapshot(cred_id, identity, balance):
    return {cred_id: {"identity": identity, "checkin": {}, "credits": balance, "error": None}}


def test_same_identity_counted_once():
    """Count one account's balance once across duplicate file paths."""
    snap = {}
    snap.update(_snapshot("/old/auth/x.info", IDENTITY, _balance(100)))
    snap.update(_snapshot("/new/auth/x.info", IDENTITY, _balance(100)))
    agg = aggregate_credits(snap)
    assert agg["remaining"] == 100.0, agg
    assert agg["used_by_quota"] == 0.0, agg
    print("✅ test_same_identity_counted_once")


def test_distinct_identities_still_sum():
    """Retain independent balances for different accounts."""
    snap = {"/auth/x.info": {"identity": IDENTITY, "credits": _balance(100)},
            "/auth/y.info": {"identity": OTHER, "credits": _balance(50)}}
    agg = aggregate_credits(snap)
    assert agg["remaining"] == 150.0, agg
    assert len(dedupe_by_identity(snap)) == 2, snap
    print("✅ test_distinct_identities_still_sum")


def test_unbound_entries_are_preserved():
    """Preserve historical entries with unknown ownership."""
    snap = {"legacy-a": {"credits": _balance(10)},
            "legacy-b": {"credits": _balance(20)},
            "no-credits": {},
            "checkin-only": {"checkin": {"date": "2026-01-01", "ok": True}}}
    assert len(dedupe_by_identity(snap)) == 4
    assert aggregate_credits(snap)["remaining"] == 30.0
    print("✅ test_unbound_entries_are_preserved")


def test_freshest_record_wins():
    """Prefer the newest balance snapshot for the same account."""
    snap = _snapshot("/old/auth/x.info", IDENTITY, _balance(900, 900, fetched_at=10.0))
    snap["/new/auth/x.info"] = {"identity": IDENTITY, "credits": _balance(10, 900, fetched_at=20.0)}
    agg = aggregate_credits(snap)
    assert agg["remaining"] == 10.0, agg
    assert agg["used_by_quota"] == 890.0, agg
    print("✅ test_freshest_record_wins")


def test_empty_record_does_not_shadow_balance():
    """Keep an existing balance over an unsynchronized duplicate entry."""
    snap = _snapshot("/old/auth/x.info", IDENTITY, _balance(120, fetched_at=10.0))
    snap["/new/auth/x.info"] = {"identity": IDENTITY, "credits": {}}
    assert aggregate_credits(snap)["remaining"] == 120.0
    # Prefer the populated balance independently of insertion order.
    flipped = dict(reversed(list(snap.items())))
    assert aggregate_credits(flipped)["remaining"] == 120.0
    print("✅ test_empty_record_does_not_shadow_balance")


def test_groups_are_not_cross_merged():
    """Preserve independent domestic and international balances."""
    snap = {"/old/x.info": {"identity": IDENTITY, "credits": _balance(100)},
            "/new/x.info": {"identity": IDENTITY, "credits": _balance(100)},
            "/old/i.info": {"identity": OTHER, "credits": _balance(30, intl=True)},
            "/new/i.info": {"identity": OTHER, "credits": _balance(30, intl=True)}}
    groups = aggregate_credits(snap)["groups"]
    assert groups["domestic"]["remaining"] == 100.0, groups
    assert groups["international"]["remaining"] == 30.0, groups
    print("✅ test_groups_are_not_cross_merged")


def test_ledger_reload_keeps_duplicate_rows():
    """Deduplicate aggregate reads without deleting persisted account rows."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "credits-ledger.json"
        ledger = CreditLedger(path)
        for cred_id in ("/moved/auth/x.info", "/current/auth/x.info"):
            ledger.bind_identity(cred_id, IDENTITY)
            ledger.update_credits(cred_id, _balance(77))
        assert len(ledger.snapshot()) == 2
        assert aggregate_credits(ledger.snapshot())["remaining"] == 77.0
        assert aggregate_credits(CreditLedger(path).snapshot())["remaining"] == 77.0
    print("✅ test_ledger_reload_keeps_duplicate_rows")


def main():
    for fn in (test_same_identity_counted_once, test_distinct_identities_still_sum,
               test_unbound_entries_are_preserved, test_freshest_record_wins,
               test_empty_record_does_not_shadow_balance, test_groups_are_not_cross_merged,
               test_ledger_reload_keeps_duplicate_rows):
        fn()
    print("\n全部通过 ✅")


if __name__ == "__main__":
    sys.exit(main())
