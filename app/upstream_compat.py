"""Normalize request schemas and recognize upstream channel-check responses."""
from __future__ import annotations

from copy import deepcopy
import json
import re
from urllib.parse import urlsplit, urlunsplit

_SCHEMA_MAPS = frozenset(("properties", "patternProperties", "$defs", "definitions",
                          "dependentSchemas", "dependencies"))
_SCHEMA_ARRAYS = frozenset(("allOf", "anyOf", "oneOf", "prefixItems", "items"))
_SCHEMA_CHILDREN = frozenset(("items", "additionalItems", "contains", "unevaluatedItems",
                              "additionalProperties", "unevaluatedProperties", "propertyNames",
                              "not", "if", "then", "else", "contentSchema"))
# Keywords whose values are schemas cannot be told apart from fields by shape alone.
_SCHEMA_VALUED = _SCHEMA_MAPS | _SCHEMA_CHILDREN
# Keywords holding arbitrary instance data are ambiguous whatever their shape.
_INSTANCE_KEYWORDS = frozenset(("const", "default"))
# Backslashes allow the code to sit in JSON that upstream embedded as an escaped string.
_CHANNEL_CODE = re.compile(r"""(['"])code\\*\1\s*:\s*\\*(['"]?)(?:11128|11-128)\\*\2(?!\d)""",
                           re.IGNORECASE)
_CHANNEL_TEXT = ("channel verification", "official client", "illegal api invocation from an unapproved channel")
_UNDERSCORE_ESCAPES = re.compile(r"\\+_")
# A schema string containing "\_" serializes as "\\_" in JSON.
_ESCAPED_UNDERSCORE = "\\\\_"


def _normalize_pattern(value):
    """Remove only unescaped backslashes before underscores, preserving escaped backslashes."""
    if not isinstance(value, str):
        return value

    def replace(match):
        text = match.group(0)
        return text[1:] if len(text) % 2 == 0 else text

    return _UNDERSCORE_ESCAPES.sub(replace, value)


def _normalize_schema_patterns(schema) -> None:
    """Normalize RE2-incompatible underscore escapes only in schema pattern positions."""
    if not isinstance(schema, dict):
        return
    pattern = schema.get("pattern")
    if isinstance(pattern, str):
        schema["pattern"] = _normalize_pattern(pattern)
    properties = schema.get("patternProperties")
    if isinstance(properties, dict):
        normalized = {}
        for key, value in properties.items():
            normalized_key = _normalize_pattern(key)
            if normalized_key in normalized:
                # allOf also accepts boolean schemas, so no colliding constraint is lost.
                value = {"allOf": [normalized[normalized_key], value]}
            normalized[normalized_key] = value
        schema["patternProperties"] = normalized
    for key in _SCHEMA_MAPS:
        value = schema.get(key)
        if isinstance(value, dict):
            for child in value.values():
                _normalize_schema_patterns(child)
    for key in _SCHEMA_ARRAYS:
        value = schema.get(key)
        if isinstance(value, list):
            for child in value:
                _normalize_schema_patterns(child)
    for key in _SCHEMA_CHILDREN:
        _normalize_schema_patterns(schema.get(key))


def _has_underscore_escape(value) -> bool:
    """Cheaply detect a backslash before an underscore anywhere in a JSON-compatible value."""
    try:
        return _ESCAPED_UNDERSCORE in json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError, RecursionError):
        return True


def _looks_like_field(value) -> bool:
    """A legacy field carries a boolean required flag, a field type or an enum."""
    return (type(value.get("required")) is bool or isinstance(value.get("type"), (str, list))
            or isinstance(value.get("enum"), list))


def _holds_field(name: str, value: dict) -> bool:
    """Whether an object under this root key is a legacy field rather than a schema keyword value."""
    if name in _INSTANCE_KEYWORDS:
        return False
    if name in _SCHEMA_VALUED:
        return _looks_like_field(value)
    # Other keywords hold scalars or arrays, so an object under them can only be a field.
    return True


def _parameter_root(parameters):
    """Wrap an empty root or a field map with legacy boolean required flags in an object schema.

    A root is legacy only when every value is a field object and at least one field carries a boolean
    ``required``. A keyword such as ``pattern`` or ``description`` holding an object is a field name;
    schema-valued keywords must also look like a field, and ``const``/``default`` never count. Anything
    else is passed through.
    """
    if not isinstance(parameters, dict):
        return parameters
    if not parameters:
        return {"type": "object", "properties": {}}
    if (any(name.startswith(("$", "x-")) for name in parameters)
            or not all(isinstance(field, dict) and type(field.get("required", False)) is bool
                       for field in parameters.values())
            or not any("required" in field for field in parameters.values())
            or not all(_holds_field(name, field) for name, field in parameters.items())):
        return parameters
    properties, required = {}, []
    for name, field in parameters.items():
        field = dict(field)
        if field.pop("required", False):
            required.append(name)
        properties[name] = field
    result = {"type": "object", "properties": properties}
    if required:
        result["required"] = required
    return result


def _normalize_declaration(declaration, *, roots: bool):
    """Return the declaration with normalized parameters, or the same object when nothing changes."""
    if not isinstance(declaration, dict) or "parameters" not in declaration:
        return declaration
    original = declaration["parameters"]
    parameters = _parameter_root(original) if roots else original
    if _has_underscore_escape(parameters):
        parameters = deepcopy(parameters)
        _normalize_schema_patterns(parameters)
    return declaration if parameters is original else {**declaration, "parameters": parameters}


def _normalize_tool(tool, key: str):
    if key == "functions":
        return _normalize_declaration(tool, roots=True)
    if not isinstance(tool, dict):
        return tool
    function = tool.get("function")
    if isinstance(function, dict) and tool.get("type", "function") == "function":
        normalized = _normalize_declaration(function, roots=True)
        if normalized is not function or "type" not in tool:
            # An explicit type lets named tool_choice and upstream recognize the declaration.
            tool = {**tool, "type": "function", "function": normalized}
    # Bare entries are not function declarations, but their patterns still reach the RE2 upstream.
    return _normalize_declaration(tool, roots=False)


def normalize_tool_schemas(body: dict) -> dict:
    """Make tool and structured-output schemas acceptable upstream, copying only what changes.

    Function parameter roots become explicit object schemas, and RE2-incompatible ``\\_`` escapes are
    removed from schema patterns. Messages, nested schemas and tool metadata are preserved.
    """
    result = body
    for key in ("tools", "functions"):
        tools = body.get(key)
        if not isinstance(tools, list):
            continue
        normalized = [_normalize_tool(tool, key) for tool in tools]
        if any(new is not old for new, old in zip(normalized, tools)):
            result = result if result is not body else dict(body)
            result[key] = normalized
    response_format = body.get("response_format")
    if isinstance(response_format, dict) and _has_underscore_escape(response_format):
        result = result if result is not body else dict(body)
        result["response_format"] = response_format = deepcopy(response_format)
        json_schema = response_format.get("json_schema")
        if isinstance(json_schema, dict):
            _normalize_schema_patterns(json_schema.get("schema"))
    return result


def is_channel_check_error(raw: bytes | str) -> bool:
    """Require both a channel-check business code and matching explanatory text."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    if not isinstance(text, str) or not _CHANNEL_CODE.search(text):
        return False
    lower = text.lower()
    return "渠道校验" in text or any(marker in lower for marker in _CHANNEL_TEXT)


def should_channel_fallback(fallback_url, status: int, raw: bytes | str) -> bool:
    """Allow one console retry only for a confirmed primary-endpoint channel rejection."""
    return bool(fallback_url) and status == 400 and is_channel_check_error(raw)


def channel_fallback_url(url: str) -> str | None:
    """Return the console chat endpoint only for the primary v2 chat endpoint."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.path != "/v2/chat/completions":
        return None
    return urlunsplit(parts._replace(path="/console/chat/completions"))
