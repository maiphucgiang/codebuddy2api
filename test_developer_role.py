#!/usr/bin/env python3
"""developer 角色归一化回归测试。

上游（copilot.tencent.com / workbuddy.ai）风控会把 role:"developer" 识别为
非官方客户端通道指纹，返回 HTTP 400 / code 11128
"Illegal API invocation from an unapproved channel"；官方 CLI/WorkBuddy 只发
"system"，而 pi 等 OpenAI 兼容 harness 把系统提示词以 "developer" 发送。

直接运行：python3 test_developer_role.py
"""

import sys
import unittest

sys.path.insert(0, ".")

import converter


def _prepare(messages, **extra):
    body = {"model": "auto", "messages": messages, "stream": False}
    body.update(extra)
    saved = dict(converter.CONFIG)
    converter.CONFIG["model_guard"] = False
    converter.CONFIG["desensitize"] = False
    try:
        return converter._prepare_chat_body(body)
    finally:
        converter.CONFIG.clear()
        converter.CONFIG.update(saved)


class DeveloperRoleNormalization(unittest.TestCase):
    def test_leading_developer_becomes_system(self):
        """pi 的典型形态：单条 developer + user，必须变成 system 且不再补占位 system。"""
        body = _prepare([
            {"role": "developer", "content": "You are an expert coding assistant."},
            {"role": "user", "content": "hi"},
        ])
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertEqual(body["messages"][0]["content"], "You are an expert coding assistant.")

    def test_developer_moved_to_front_when_not_first(self):
        """developer 不在首位时，归一化后仍应被搬到首条 system 位置。"""
        body = _prepare([
            {"role": "user", "content": "hi"},
            {"role": "developer", "content": "rules"},
        ])
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertEqual(body["messages"][0]["content"], "rules")

    def test_existing_system_first_is_untouched(self):
        """workbuddy 形态：本来就是 system，行为不变。"""
        body = _prepare([
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "hi"},
        ])
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertEqual(body["messages"][0]["content"], "You are a helpful assistant.")

    def test_no_developer_role_leaks_upstream(self):
        """任意组合下，发往上游的 messages 都不允许再出现 developer。"""
        body = _prepare([
            {"role": "developer", "content": "a"},
            {"role": "user", "content": "b"},
            {"role": "assistant", "content": "c"},
            {"role": "developer", "content": "d"},
        ])
        self.assertNotIn("developer", [m["role"] for m in body["messages"]])

    def test_missing_system_still_gets_placeholder(self):
        """完全没有 system/developer 时，仍保留原有占位逻辑。"""
        body = _prepare([{"role": "user", "content": "hi"}])
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(body["messages"][0]["content"], "You are a helpful assistant.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
