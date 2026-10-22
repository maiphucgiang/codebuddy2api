"""Normalize legacy parameter roots with synthetic offline schema and route fixtures."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from copy import deepcopy
import unittest
from unittest.mock import patch

import converter
from app.upstream_compat import normalize_tool_schemas
import test_region_routing as fixtures


def tool(name, parameters):
    return {"type": "function", "function": {"name": name, "parameters": parameters}}


def legacy_tools():
    return [tool("interrupt_worker", {"target": {"type": "string", "required": True}}),
            tool("list_workers", {})]


def expected_tools():
    return [tool("interrupt_worker", {"type": "object", "properties": {"target": {"type": "string"}},
                                     "required": ["target"]}),
            tool("list_workers", {"type": "object", "properties": {}})]


class ToolSchemaTests(unittest.TestCase):
    def test_legacy_roots_are_normalized_without_mutating_the_request(self):
        body = {"tools": legacy_tools(), "messages": [{"role": "user", "content": "keep text"}]}
        before = deepcopy(body)
        result = normalize_tool_schemas(body)
        self.assertEqual(result["tools"], expected_tools())
        self.assertEqual(body, before)
        self.assertIs(result["messages"], body["messages"])
        self.assertIs(normalize_tool_schemas(result), result)

    def test_preserves_field_constraints_metadata_and_required_order(self):
        nested = {"type": "string", "enum": ["x", "y"], "pattern": "^[a-z]+$", "default": "x"}
        fields = {"target": {**nested, "required": True}, "optional": {"type": "integer", "required": False},
                  "values": {"type": "array", "items": nested, "required": True},
                  "options": {"type": "object", "properties": {"value": nested}}}
        body = {"tools": [tool("example", fields)]}
        body["tools"][0]["function"].update(description="Keep metadata", strict=False)
        before = deepcopy(body)
        result = normalize_tool_schemas(body)
        schema = result["tools"][0]["function"]["parameters"]
        self.assertEqual(schema["required"], ["target", "values"])
        self.assertEqual(schema["properties"]["target"], nested)
        self.assertNotIn("required", schema["properties"]["optional"])
        self.assertIs(schema["properties"]["values"]["items"], nested)
        self.assertEqual(result["tools"][0]["function"]["description"], "Keep metadata")
        self.assertIs(result["tools"][0]["function"]["strict"], False)
        self.assertEqual(body, before)

    def test_optional_only_map_has_no_root_required(self):
        result = normalize_tool_schemas({"tools": [tool("optional", {"value": {"type": "string", "required": False}})]})
        self.assertEqual(result["tools"][0]["function"]["parameters"],
                         {"type": "object", "properties": {"value": {"type": "string"}}})

    def test_standard_schema_and_nested_empty_schemas_are_untouched(self):
        schema = {"type": "object", "properties": {
            "free": {}, "choice": {"oneOf": [{"type": "string"}, {"type": "object", "properties": {}}]},
            "record": {"type": "object", "additionalProperties": {"type": "string"}}},
            "required": ["free"], "additionalProperties": False, "x-vendor": {"keep": True}}
        body = {"tools": [tool("standard", schema)]}
        self.assertIs(normalize_tool_schemas(body), body)
        self.assertIs(body["tools"][0]["function"]["parameters"], schema)

    def test_ambiguous_or_incomplete_standard_roots_are_not_guessed(self):
        schemas = [{"$ref": "#/$defs/X"}, {"anyOf": [{"type": "object"}]}, {"properties": {}},
                   {"patternProperties": {"x": {"type": "string"}}}, {"format": {"type": "string"}},
                   {"value": {"type": "unknown"}}, {"value": {"type": ["string", "null"]}},
                   {"value": {"type": "string", "required": ["nested"]}},
                   {"value": {"type": "string", "required": 1}}, {"value": {}},
                   {"value": {"type": "string"}}, {"x-vendor": {"type": "string"}},
                   {"x-vendor": {"type": "string", "required": True}},
                   {"value": {"type": "string", "required": True}, "x-extension": {"type": "string"}},
                   {"value": {"type": "string"}, "x-extension": "keep"}, True, False, None, []]
        for schema in schemas:
            with self.subTest(schema=schema):
                body = {"tools": [tool("untouched", schema)]}
                self.assertIs(normalize_tool_schemas(body), body)

    def test_schema_keyword_field_names_do_not_hide_a_legacy_map(self):
        fields = {"pattern": {"type": "string", "required": True}, "path": {"type": "string"},
                  "description": {"type": "string", "required": False}}
        result = normalize_tool_schemas({"tools": [tool("grep", fields)]})
        self.assertEqual(result["tools"][0]["function"]["parameters"], {
            "type": "object", "required": ["pattern"], "properties": {
                "pattern": {"type": "string"}, "path": {"type": "string"},
                "description": {"type": "string"}}})

    def test_one_required_flag_is_enough_for_fields_without_a_single_type(self):
        fields = {"target": {"type": "string", "required": True}, "note": {"type": ["string", "null"]},
                  "mode": {"enum": ["a", "b"]}}
        result = normalize_tool_schemas({"tools": [tool("mixed", fields)]})
        self.assertEqual(result["tools"][0]["function"]["parameters"], {
            "type": "object", "required": ["target"], "properties": {
                "target": {"type": "string"}, "note": {"type": ["string", "null"]},
                "mode": {"enum": ["a", "b"]}}})

    def test_schema_keywords_holding_schema_values_keep_the_root_untouched(self):
        for schema in [{"properties": {"a": {"type": "string"}}, "b": {"type": "string", "required": True}},
                       {"description": "keep", "b": {"type": "string", "required": True}}]:
            with self.subTest(schema=schema):
                body = {"tools": [tool("standard", schema)]}
                self.assertIs(normalize_tool_schemas(body), body)

    def test_instance_data_keywords_keep_the_root_untouched(self):
        for schema in [{"const": {"type": "object", "required": True}},
                       {"default": {"type": "string", "required": True}, "b": {"type": "string"}}]:
            with self.subTest(schema=schema):
                body = {"tools": [tool("standard", schema)]}
                self.assertIs(normalize_tool_schemas(body), body)

    def test_enum_only_fields_under_schema_keywords_are_fields(self):
        fields = {"items": {"enum": ["a", "b"]}, "format": {"enum": ["json", "text"]},
                  "target": {"type": "string", "required": True}}
        result = normalize_tool_schemas({"tools": [tool("export", fields)]})
        self.assertEqual(result["tools"][0]["function"]["parameters"], {
            "type": "object", "required": ["target"], "properties": {
                "items": {"enum": ["a", "b"]}, "format": {"enum": ["json", "text"]},
                "target": {"type": "string"}}})

    def test_function_entries_without_a_type_are_normalized(self):
        body = {"tools": [{"function": {"name": "list_workers", "parameters": {}}},
                          {"function": {"name": "standard", "parameters": {"type": "object"}}}]}
        result = normalize_tool_schemas(body)
        self.assertEqual(result["tools"], [tool("list_workers", {"type": "object", "properties": {}}),
                                           tool("standard", {"type": "object"})])
        self.assertNotIn("type", body["tools"][0])

    def test_named_choice_selects_a_function_entry_without_a_type(self):
        body = {"model": "auto", "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"function": {"name": "list_workers", "parameters": {}}}],
                "tool_choice": {"type": "function", "function": {"name": "list_workers"}}}
        with patch.dict(converter.CONFIG, {"model_guard": False, "desensitize": False}):
            result = converter._prepare_chat_body(body)
        self.assertEqual(result["tools"], [tool("list_workers", {"type": "object", "properties": {}})])
        self.assertEqual(result["tool_choice"], "required")

    def test_legacy_roots_and_pattern_escapes_are_normalized_together(self):
        fields = {"run_id": {"type": "string", "pattern": r"^agent\_run\_", "required": True}}
        body = {"tools": [tool("agent_run", fields)]}
        before = deepcopy(body)
        result = normalize_tool_schemas(body)
        self.assertEqual(result["tools"][0]["function"]["parameters"], {
            "type": "object", "required": ["run_id"],
            "properties": {"run_id": {"type": "string", "pattern": "^agent_run_"}}})
        self.assertEqual(body, before)

    def test_non_function_tools_and_missing_parameters_are_untouched(self):
        for entry in [None, "unknown", {"type": "custom", "parameters": {}},
                      {"type": "function"}, {"type": "function", "function": {"name": "no_schema"}}]:
            with self.subTest(entry=entry):
                body = {"tools": [entry]}
                self.assertIs(normalize_tool_schemas(body), body)
        for body in [{}, {"tools": None}, {"functions": {}}, {"tools": []}]:
            self.assertIs(normalize_tool_schemas(body), body)

    def test_legacy_functions_and_mixed_declarations(self):
        valid = tool("valid", {"type": "object", "properties": {}})
        body = {"tools": [valid, *legacy_tools()], "functions": [
            {"name": "flat", "parameters": {"target": {"type": "string", "required": True}}}]}
        before = deepcopy(body)
        result = normalize_tool_schemas(body)
        self.assertIs(result["tools"][0], valid)
        self.assertEqual(result["tools"][1:], expected_tools())
        self.assertEqual(result["functions"][0]["parameters"], expected_tools()[0]["function"]["parameters"])
        self.assertEqual(body, before)

    def test_preparation_normalizes_named_choice_before_budget_check(self):
        body = {"model": "auto", "messages": [{"role": "user", "content": "hi"}],
                "tools": legacy_tools(), "tool_choice": {"type": "function", "function": {"name": "list_workers"}}}
        before = deepcopy(body)
        with patch.dict(converter.CONFIG, {"model_guard": False, "desensitize": False}), \
             patch.object(converter, "_guard_request_size") as guard:
            result = converter._prepare_chat_body(body)
        self.assertEqual(result["tools"], expected_tools()[1:])
        self.assertEqual(result["tool_choice"], "required")
        self.assertEqual(guard.call_args.args[0]["tools"], expected_tools()[1:])
        self.assertEqual(body, before)


class ToolSchemaEndpointTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixtures.RegionRoutingTests()
        self.addCleanup(self.fx.doCleanups)
        self.fx.setUp()
        self.enterContext(patch.object(converter, "_log"))

    def test_all_protocols_profiles_and_streaming_send_standard_roots(self):
        for profile in fixtures.PROFILES:
            for endpoint in fixtures.GENERATIONS:
                for stream in (False, True):
                    for desensitize in (False, True):
                        with self.subTest(profile=profile, endpoint=endpoint, stream=stream,
                                          desensitize=desensitize):
                            self.fx.configure(profiles=(profile,))
                            converter.CONFIG["desensitize"] = desensitize
                            payload = self.fx.payload(endpoint, stream=stream)
                            if endpoint == "responses":
                                payload["tools"] = [{"type": "function", **entry["function"]}
                                                    for entry in legacy_tools()]
                            elif endpoint == "messages":
                                payload["tools"] = [{"name": entry["function"]["name"],
                                                     "input_schema": entry["function"]["parameters"]}
                                                    for entry in legacy_tools()]
                            else:
                                payload["tools"] = legacy_tools()
                            before = deepcopy(payload)
                            _, upstream = self.fx.post_ok(endpoint, payload, {profile})
                            self.assertEqual(upstream["tools"], expected_tools())
                            self.assertEqual(payload, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
