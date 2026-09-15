#!/usr/bin/env python3
"""Normalize developer roles for upstream compatibility without mutating caller messages."""

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import converter


def _prepare(messages, *, desensitize=False, **extra):
    body = {"model": "auto", "messages": messages, "stream": False}
    body.update(extra)
    saved = dict(converter.CONFIG)
    converter.CONFIG["model_guard"] = False
    converter.CONFIG["desensitize"] = desensitize
    converter.CONFIG["no_compact"] = False
    try:
        return converter._prepare_chat_body(body)
    finally:
        converter.CONFIG.clear()
        converter.CONFIG.update(saved)


class DeveloperRoleNormalization(unittest.TestCase):
    def test_leading_developer_becomes_system(self):
        """Convert leading developer instructions without adding duplicate system messages."""
        body = _prepare([
            {"role": "developer", "content": "You are an expert coding assistant."},
            {"role": "user", "content": "hi"},
        ])
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertEqual(body["messages"][0]["content"], "You are an expert coding assistant.")

    def test_developer_moved_to_front_when_not_first(self):
        """Move normalized developer instructions to the first system position."""
        body = _prepare([
            {"role": "user", "content": "hi"},
            {"role": "developer", "content": "rules"},
        ])
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertEqual(body["messages"][0]["content"], "rules")

    def test_existing_system_first_is_untouched(self):
        """Preserve existing system-message behavior."""
        body = _prepare([
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "hi"},
        ])
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertEqual(body["messages"][0]["content"], "You are a helpful assistant.")

    def test_no_developer_role_leaks_upstream(self):
        """Never send developer roles upstream."""
        body = _prepare([
            {"role": "developer", "content": "a"},
            {"role": "user", "content": "b"},
            {"role": "assistant", "content": "c"},
            {"role": "developer", "content": "d"},
        ])
        self.assertNotIn("developer", [m["role"] for m in body["messages"]])

    def test_missing_system_still_gets_placeholder(self):
        """Retain placeholder instructions when no system or developer message exists."""
        body = _prepare([{"role": "user", "content": "hi"}])
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(body["messages"][0]["content"], "You are a helpful assistant.")


class CallerPayloadNotMutated(unittest.TestCase):
    """Normalize upstream copies without mutating caller-owned payloads."""

    def _assert_untouched(self, raw_messages, *, desensitize):
        raw_body = {"model": "auto", "messages": raw_messages, "stream": False}
        snapshot = copy.deepcopy(raw_body)
        _prepare(raw_messages, desensitize=desensitize)
        self.assertEqual(raw_body, snapshot)

    def test_string_content_untouched(self):
        self._assert_untouched([
            {"role": "developer", "content": "You are an expert coding assistant."},
            {"role": "user", "content": "hi"},
        ], desensitize=False)

    def test_string_content_untouched_with_desensitize(self):
        self._assert_untouched([
            {"role": "developer", "content": "You are an expert coding assistant."},
            {"role": "user", "content": "hi"},
        ], desensitize=True)

    def test_text_blocks_untouched(self):
        self._assert_untouched([
            {"role": "developer", "content": [{"type": "text", "text": "You are pi."}]},
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        ], desensitize=False)

    def test_text_blocks_untouched_with_desensitize(self):
        self._assert_untouched([
            {"role": "developer", "content": [{"type": "text", "text": "You are pi."}]},
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        ], desensitize=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
