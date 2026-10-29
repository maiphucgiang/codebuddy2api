"""Measured billing for catalog models that upstream declares free.

Upstream reports the credit actually charged on every response's ``usage``; the audit
log keeps it per request. A model whose catalog entry says ``x0.00`` but whose recent
requests were charged is "nominally free": routing must treat it as paid.

Traffic proves *that* a model is billed, not *how much* per token: upstream caches the
prompt per conversation and bills a hit at about a twentieth of fresh input, and those
hits are often missing from the reported usage. Dividing traffic charges by any token
price therefore under-reads the rate by an order of magnitude (measured 2026-10-19:
traffic suggested ≈0.04x for hy4-preview-f, a controlled probe gave ≈0.95x).

A rate is only derived from a calibrated probe: the same fresh-conversation request is
sent to the target and to a reference model with a declared paid rate on the same
product, and the target's rate is the reference rate scaled by their per-token charges.
That needs no assumed token price, and both requests miss the cache the same way.
"""
from __future__ import annotations

import json
import math
import secrets
import threading
import time

import httpx

from .observability import normalize_usage
from .site_routing import profile_for_headers
from .upstream_compat import channel_fallback_url
from .upstream_io import open_backend_stream_sync

WINDOW_S = 7 * 86400
# A model counts as billed when any of its latest samples was charged; a returning
# free period therefore needs this many uncharged requests before it shows free again.
RECENT = 20
PROFILES = ("cn-cli", "cn-work", "intl-cli", "intl-work")
# Reference models near this declared rate charge a few billing units per probe: enough
# resolution against the 0.01 unit without spending much.
REFERENCE_RATE = 0.1


def summarize(samples):
    """Group billing samples by (profile, model) into the measurement each key supports.

    Only probe samples carry a calibrated ``rate``; the newest one in the window wins.
    """
    grouped = {}
    for sample in samples:
        grouped.setdefault((sample["profile"], sample["model"]), []).append(sample)
    out = {}
    for key, rows in grouped.items():
        rows.sort(key=lambda row: row["started_at"])
        recent = rows[-RECENT:]
        charged = [row for row in rows if row["credit"] > 0]
        rated = [row for row in rows if row.get("rate") is not None]
        out[key] = {
            "billed": any(row["credit"] > 0 for row in recent),
            "rate": rated[-1]["rate"] if rated else None,
            "rated_at": rated[-1]["started_at"] if rated else None,
            "samples": len(rows),
            "charged": len(charged),
            "avg_credit": round(sum(row["credit"] for row in charged) / len(charged), 4) if charged else None,
            "last_charged_at": charged[-1]["started_at"] if charged else None,
            "last_sample_at": rows[-1]["started_at"],
        }
    return out


def calibrated_rate(target, reference, reference_rate):
    """Target rate from two same-shape probes; None when the reference charge is too coarse."""
    if reference["credit"] <= 0 or target["input_tokens"] <= 0 or reference["input_tokens"] <= 0:
        return None
    per_token = target["credit"] / target["input_tokens"]
    reference_per_token = reference["credit"] / reference["input_tokens"]
    return round(reference_rate * per_token / reference_per_token, 4)


def reference_candidates(items, parse_rate, exclude):
    """Declared paid models as (model, rate), nearest REFERENCE_RATE first on a log scale.

    Catalogs list models an account's tier may not call, so callers try several in turn.
    """
    paid = [(rate, item["id"]) for item in items
            if item.get("id") and item["id"] != exclude
            and (rate := parse_rate(item.get("credits"))) is not None and rate > 0]
    paid.sort(key=lambda pair: (abs(math.log(pair[0] / REFERENCE_RATE)), pair[1]))
    return [(model, rate) for rate, model in paid]


def rate_label(rate):
    """Catalog-style rate string that the gateway's multiplier parser reads back."""
    text = f"{max(rate, 0.0):.4f}".rstrip("0").rstrip(".")
    return f"x{text if '.' in text else text + '.0'}"


class RateBook:
    """Effective model rates: manual override, then measured billing, then the catalog."""

    def __init__(self, *, manual=lambda: {}):
        self._manual = manual          # () -> {(profile, model): rate}; cheap and revision-cached by caller
        self._lock = threading.Lock()
        self._measured = {}
        self._billed_profiles = frozenset()   # Lets the hot path skip products with nothing billed.
        self.revision = 0
        self.refreshed_at = None

    def refresh(self, samples):
        """Republish measurements; returns True when routing-relevant facts changed."""
        measured = summarize(samples)
        with self._lock:
            before = {key: (row["billed"], row["rate"]) for key, row in self._measured.items()}
            after = {key: (row["billed"], row["rate"]) for key, row in measured.items()}
            self._measured = measured
            self._billed_profiles = frozenset(key[0] for key, row in measured.items() if row["billed"])
            self.refreshed_at = time.time()
            if before != after:
                self.revision += 1
                return True
            return False

    def measurement(self, profile, model):
        with self._lock:
            row = self._measured.get((profile, model))
            return dict(row) if row else None

    def measurements(self):
        with self._lock:
            return {key: dict(row) for key, row in self._measured.items()}

    def overlay(self, profile, items, is_free):
        """Return catalog items with effective rates; untouched items are shared, not copied."""
        if items is None or profile not in PROFILES:
            return items
        manual = self._manual()
        with self._lock:
            measured, billed = self._measured, self._billed_profiles
        # Most products have nothing to overlay; hand back the shared list untouched.
        if profile not in billed and not any(key[0] == profile for key in manual):
            return items
        out = []
        for item in items:
            model = item.get("id")
            rate, source = manual.get((profile, model)), "manual"
            if rate is None:
                row = measured.get((profile, model))
                if row and row["billed"] and is_free(item.get("credits")):
                    # A charged model must never round back to a free-looking x0.0.
                    rate = None if row["rate"] is None else max(row["rate"], 0.0001)
                    source = "measured"
                else:
                    out.append(item)
                    continue
            # An unknown measured rate stays non-free: no credits string parses as zero.
            out.append({**item, "credits": rate_label(rate) if rate is not None else None,
                        "credits_declared": item.get("credits"), "credits_source": source})
        return out


# ---------------------------------------------------------------------------
# One-request probe
# ---------------------------------------------------------------------------

PROBE_LINES = 680   # ≈ 8K input tokens: enough that a ≈0.03x rate charges several billing units


def probe_prompt():
    """A fixed-size prompt behind a random nonce, so no earlier probe is a cache hit."""
    lines = "\n".join(f"{index:04d} the quick brown fox jumps over the lazy dog" for index in range(PROBE_LINES))
    return f"[{secrets.token_hex(8)}]\n{lines}\n\nReply with the single word OK."


class ProbeError(RuntimeError):
    """A probe refused upstream, keeping the status and business code for the request log."""

    def __init__(self, message, status_code=None, code=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _upstream_error(response, raw=None):
    """Status plus upstream's own code and message, which say why a model was refused.

    ``raw`` holds error bytes already consumed from the stream; None means the body is still unread.
    """
    try:
        data = json.loads((response.read() if raw is None else raw)[:4096])
    except (ValueError, httpx.HTTPError):
        data = None
    code = data.get("code") if isinstance(data, dict) else None
    if isinstance(data, dict) and (code is not None or data.get("msg")):
        message = f"上游 HTTP {response.status_code} code={code} {str(data.get('msg') or '')[:120]}"
    else:
        message = f"上游 HTTP {response.status_code}"
    return ProbeError(message, response.status_code, None if code is None else str(code))


def probe_once(url, headers, model, *, timeout=120.0):
    """Send one real request and return the upstream-reported billing sample."""
    body = {"model": model, "stream": True, "stream_options": {"include_usage": True}, "max_tokens": 16,
            "messages": [{"role": "system", "content": "You are a helpful assistant."},
                         {"role": "user", "content": probe_prompt()}]}
    usage = None
    request_headers = {**headers, "Accept": "text/event-stream"}
    if profile_for_headers(headers) == "cn-work":
        request_headers.update({"X-Agent-Type": "auxiliary", "X-Agent-Runtime": "local-agent"})
    with httpx.Client(follow_redirects=False, timeout=httpx.Timeout(timeout, connect=10)) as client:
        with open_backend_stream_sync(
                client, url, request_headers, body,
                fallback_url=channel_fallback_url(url)) as (response, raw):
            if response.status_code != 200:
                raise _upstream_error(response, raw)
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    event = json.loads(payload)
                except ValueError:
                    continue
                if isinstance(event, dict) and isinstance(event.get("usage"), dict):
                    usage = event["usage"]
    clean = normalize_usage(usage)
    if "credit" not in clean:
        raise ProbeError("上游响应未返回计费 credit", 200, "credit_missing")
    return {"credit": clean["credit"], "input_tokens": int(clean.get("input_tokens") or 0),
            "cache_read_tokens": int(clean.get("cache_read_tokens") or 0),
            "output_tokens": int(clean.get("output_tokens") or 0)}
