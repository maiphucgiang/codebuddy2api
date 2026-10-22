"""Adapt empty tool roots and field maps with explicit legacy required flags."""
from __future__ import annotations

# A root containing schema keywords is not a legacy property-name -> schema map.
_SCHEMA_KEYS = frozenset((
    "$schema", "$id", "$ref", "$anchor", "$dynamicRef", "$dynamicAnchor", "$defs",
    "$vocabulary", "$comment", "definitions", "type", "properties", "patternProperties",
    "additionalProperties", "unevaluatedProperties", "propertyNames", "required",
    "dependentRequired", "dependentSchemas", "dependencies", "minProperties", "maxProperties",
    "allOf", "anyOf", "oneOf", "not", "if", "then", "else", "items", "prefixItems",
    "additionalItems", "contains", "unevaluatedItems", "minItems", "maxItems", "uniqueItems",
    "minContains", "maxContains", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "multipleOf", "minLength", "maxLength", "pattern", "format", "contentEncoding",
    "contentMediaType", "contentSchema", "enum", "const", "title", "description", "default",
    "examples", "deprecated", "readOnly", "writeOnly",
))
_TYPES = frozenset(("string", "number", "integer", "boolean", "object", "array", "null"))


def _parameter_root(parameters):
    if not isinstance(parameters, dict):
        return parameters
    if not parameters:
        return {"type": "object", "properties": {}}
    if (_SCHEMA_KEYS.intersection(parameters)
            or any(name.startswith(("$", "x-")) for name in parameters)
            or not any(isinstance(field, dict) and type(field.get("required")) is bool
                       for field in parameters.values())):
        return parameters
    if not all(
        isinstance(field, dict) and isinstance(field.get("type"), str) and field["type"] in _TYPES
        and ("required" not in field or type(field["required"]) is bool)
        for field in parameters.values()
    ):
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


def normalize_tool_parameter_roots(body: dict) -> dict:
    """Copy only changed function declarations; preserve messages and nested schemas.

    Some clients send an empty root or a legacy parameter map with per-field
    boolean ``required`` flags. Upstreams require an explicit object root.
    Ambiguous/incomplete standard schemas are deliberately left untouched.
    """
    result = body
    for key in ("tools", "functions"):
        tools = body.get(key)
        if not isinstance(tools, list):
            continue
        normalized, changed = [], False
        for tool in tools:
            if not isinstance(tool, dict) or (key == "tools" and tool.get("type") != "function"):
                normalized.append(tool)
                continue
            function = tool.get("function") if key == "tools" else tool
            if not isinstance(function, dict) or "parameters" not in function:
                normalized.append(tool)
                continue
            original = function["parameters"]
            parameters = _parameter_root(original)
            if parameters is original:
                normalized.append(tool)
                continue
            function = {**function, "parameters": parameters}
            normalized.append({**tool, "function": function} if key == "tools" else function)
            changed = True
        if changed:
            if result is body:
                result = dict(body)
            result[key] = normalized
    return result
