"""Bounded, dependency-free log previews; never use these helpers for wire data.

Only common credential fields/token shapes are recognized, not arbitrary secrets.
Text inspection is limited to the first ``max_bytes`` characters. Structured
previews additionally cap string length, depth, total nodes and container width;
truncated previews need not be valid JSON. No image data is decoded.
"""

import itertools
import json
import re

__all__ = ["format_log_body", "sanitize_log_text"]

_REDACTED = "[REDACTED]"
_TRUNCATED = "...[truncated]"
_MAX_STRING_CHARS = 4096
_MAX_KEY_CHARS = 128
_MAX_DEPTH = 8
_MAX_ITEMS = 32
_MAX_NODES = 512

_SENSITIVE_KEYS = frozenset({
    "authorization", "proxyauthorization", "apikey", "apikeys", "xapikey",
    "accesstoken", "refreshtoken", "idtoken", "authtoken", "bearertoken",
    "password", "passwd", "pwd", "cookie", "cookies", "setcookie",
    "clientsecret", "secret", "secretkey", "privatekey", "token",
})
_KEY_NAMES = (
    r"(?:proxy[-_ ]*)?authorization|(?:x[-_ ]*)?api[-_ ]*keys?|"
    r"(?:access|refresh|id|auth|bearer)[-_ ]*token|password|passwd|pwd|"
    r"(?:set[-_ ]*)?cookies?|client[-_ ]*secret|secret[-_ ]*key|"
    r"private[-_ ]*key|secret|token"
)
_ASSIGNMENT = re.compile(
    r"(?<![\w-])[\"']?(?P<key>" + _KEY_NAMES + r")[\"']?\s*[:=]\s*",
    re.IGNORECASE,
)
_SCHEME_VALUE = re.compile(r"(?:Bearer|Basic)\s+[^\s\"'<>;,}\]]+", re.IGNORECASE)
_BARE_VALUE = re.compile(r"[^\s,;&}\]]+")
_COOKIE_VALUE = re.compile(r"[^\s,;&}\]]+(?:[ \t]*;[ \t]*[^\s,;&}\]]+)*")
_AUTH_SCHEME = re.compile(r"\b(Bearer|Basic)\s+[^\s\"'<>;,}]+", re.IGNORECASE)
_IMAGE_URL = re.compile(
    r"data:(?P<mime>image(?:/|\\/)[a-z0-9.+-]+)"
    r"(?:;[^,;\s\"'<>\\]+)*;base64\s*,[^\s\"'<>]*",
    re.IGNORECASE,
)
# Match even incomplete tokens at the inspection boundary. Do not redact generic
# hex strings/UUIDs: those often carry useful request and trace identifiers.
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]*){0,2}")
_API_TOKEN = re.compile(
    r"(?<![\w-])(?:sk-[A-Za-z0-9_-]*|(?:sk|rk)_(?:live|test)_[A-Za-z0-9_]*|"
    r"AIza[A-Za-z0-9_-]*|(?:AKIA|ASIA)[A-Z0-9]*|"
    r"gh[pousr]_[A-Za-z0-9_]*|github_pat_[A-Za-z0-9_]*|"
    r"xox[baprs]-[A-Za-z0-9-]*)"
)


def _check_limit(max_bytes):
    if not isinstance(max_bytes, int):
        raise TypeError("max_bytes must be an integer")
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")


def _normalized_key(key):
    # Avoid allocating/scanning a second copy of a giant, attacker-controlled key.
    if not isinstance(key, str) or len(key) > _MAX_KEY_CHARS:
        return ""
    return re.sub(r"[-_\s]", "", key).lower()


def _quoted_end(text, start):
    quote = text[start]
    index = start + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
        elif text[index] == quote:
            return index + 1
        else:
            index += 1
    return len(text)


def _secret_end(text, start, key):
    if start == len(text):
        return start
    if text[start] in "\"'":
        return _quoted_end(text, start)
    if text[start] in "[{":
        # A counter suffices for a preview, including malformed/incomplete JSON.
        # Never recurse into secret containers or expose their child values.
        depth = 0
        index = start
        while index < len(text):
            char = text[index]
            if char in "\"'":
                index = _quoted_end(text, index)
                continue
            if char in "[{":
                depth += 1
            elif char in "]}":
                depth -= 1
                if depth == 0:
                    return index + 1
            index += 1
        return len(text)
    pattern = _COOKIE_VALUE if key in {"cookie", "cookies", "setcookie"} else _BARE_VALUE
    match = _SCHEME_VALUE.match(text, start) or pattern.match(text, start)
    return match.end() if match else start


def _redact_assignments(text):
    parts = []
    end = 0
    for match in _ASSIGNMENT.finditer(text):
        if match.start() < end:
            continue
        parts.append(text[end:match.end()])
        start = match.end()
        end = _secret_end(text, start, _normalized_key(match.group("key")))
        quote = text[start:start + 1]
        parts.append(quote + _REDACTED + quote if quote in {"\"", "'"} else _REDACTED)
    parts.append(text[end:])
    return "".join(parts)


def _redact_preview(text):
    text = _redact_assignments(text)
    text = _IMAGE_URL.sub(lambda m: "<" + m.group("mime") + "; base64 redacted>", text)
    text = _AUTH_SCHEME.sub(lambda m: m.group(1) + " " + _REDACTED, text)
    text = _JWT.sub(_REDACTED, text)
    return _API_TOKEN.sub(_REDACTED, text)


class _Writer:
    """Only encode bounded fragments, reserving the marker on overflow."""

    def __init__(self, limit):
        self.limit = limit
        self.remaining = limit
        self.parts = []
        self.truncated = False

    def write(self, text):
        if self.truncated:
            return
        prefix = text[:self.remaining]
        raw = prefix.encode("utf-8", errors="replace")
        if len(raw) > self.remaining or len(prefix) < len(text):
            self.truncated = True
        chunk = raw[:self.remaining]
        self.parts.append(chunk)
        self.remaining -= len(chunk)

    def finish(self, truncated=False):
        raw = b"".join(self.parts)
        if self.truncated or truncated:
            marker = _TRUNCATED.encode("ascii")[:self.limit]
            prefix = raw[:self.limit - len(marker)].decode("utf-8", errors="ignore")
            return prefix + marker.decode("ascii")
        return raw.decode("utf-8", errors="replace")


def sanitize_log_text(text: str, max_bytes: int = 65536) -> str:
    """Redact common text credentials and cap the entire UTF-8 result.

    Zero disables body output; negative/non-integer limits raise ValueError /
    TypeError. Work is bounded by the supplied limit, not the original text
    length. Any inspected-but-incomplete credential is redacted before clipping.
    Normal error types, HTTP statuses and request IDs are not token patterns.
    """
    _check_limit(max_bytes)
    if max_bytes == 0:
        return ""
    preview = text[:max_bytes]
    writer = _Writer(max_bytes)
    writer.write(_redact_preview(preview))
    return writer.finish(truncated=len(preview) < len(text))


def format_log_body(value, max_bytes: int = 65536) -> str:
    """Return a non-mutating, bounded JSON-like preview of a JSON value.

    Credential fields are replaced without visiting their values. Large strings
    retain only a sanitized prefix and character count; wide/deep containers
    retain a few children and a count/limit summary. No whole-body serialization,
    deep copy, image decoding, custom repr calls or external state is involved.
    The output (including all truncation markers) fits ``max_bytes`` UTF-8 bytes.
    """
    _check_limit(max_bytes)
    if max_bytes == 0:
        return ""
    writer = _Writer(max_bytes)
    active = set()
    nodes = 0

    def string_preview(text):
        cap = min(_MAX_STRING_CHARS, writer.remaining)
        preview = _redact_preview(text[:cap])
        if len(text) > cap:
            preview += f"...[truncated: {len(text)} chars total]"
        writer.write(json.dumps(preview, ensure_ascii=False))

    def render(item, depth):
        nonlocal nodes
        if writer.truncated:
            return
        nodes += 1
        if isinstance(item, str):
            string_preview(item)
        elif item is None or isinstance(item, (bool, float)):
            writer.write(json.dumps(item))
        elif isinstance(item, int):
            bits = item.bit_length()
            writer.write(f"<integer: {bits} bits>" if bits > 4096 else str(item))
        elif isinstance(item, (dict, list, tuple)):
            mapping = isinstance(item, dict)
            kind = "fields" if mapping else "items"
            count = len(item)
            if id(item) in active:
                writer.write("<circular reference>")
                return
            if depth >= _MAX_DEPTH:
                writer.write(f"<truncated: {count} {kind}; depth limit>")
                return
            active.add(id(item))
            writer.write("{" if mapping else "[")
            entries = iter(item.items()) if mapping else enumerate(item)
            shown = 0
            for key, child in itertools.islice(entries, _MAX_ITEMS):
                if writer.truncated or nodes >= _MAX_NODES:
                    break
                if shown:
                    writer.write(", ")
                if mapping:
                    if isinstance(key, str):
                        string_preview(key)
                    else:
                        # JSON object keys are strings; don't call custom repr.
                        writer.write('"<non-string key>"')
                    writer.write(": ")
                if mapping and _normalized_key(key) in _SENSITIVE_KEYS:
                    writer.write('"' + _REDACTED + '"')
                else:
                    render(child, depth + 1)
                shown += 1
            if shown < count:
                if shown:
                    writer.write(", ")
                writer.write(f"<truncated: {count - shown} more {kind}>")
            writer.write("}" if mapping else "]")
            active.remove(id(item))
        else:
            writer.write("<unsupported value>")

    render(value, 0)
    return writer.finish()
