#!/usr/bin/env python3
"""Every audit event the backend can emit must have a WebUI label.

The logs page shows an unmapped action as its raw code (``model.rate_probe``), and new
events kept shipping without one. This test reads the backend source instead of a
hand-kept list: literal actions come from the emit calls themselves, and each dynamic
family is expanded from the literal that bounds it in code (the PATCH field sets, the
credential action allowlist, the runtime log components, the buddy stage helpers,
the credit change types).
A dynamic emit site with no known expansion fails, so a new family cannot slip by.
"""
import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # Allow direct execution.

from app.audit_store import CREDIT_CHANGES  # noqa: E402 - sys.path is prepared above

SOURCES = sorted([*(ROOT / "app").glob("*.py"), ROOT / "converter.py"])
# Local helpers whose first stage argument becomes "buddy." + stage, and admin_api's
# event(action) wrapper, which takes the whole action. growth_events.event() builds
# telemetry payloads for upstream, not audit records, and is deliberately absent.
HELPERS = {
    "admin_api.py": {"event": (0, "")},
    "buddy.py": {"event": (0, "buddy."), "audit_event": (3, "buddy.")},
    "buddy_task.py": {"event": (0, "buddy.")},
    "travel.py": {"record_departure": (0, "buddy.")},
}
# The helpers' own bodies forward their parameter; the values are collected at call sites.
FORWARDERS = {("admin_api.py", "event"), ("buddy.py", "event")}


def _string(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _sets_of_strings(tree):
    """Every set literal made only of strings, in source order."""
    return [{_string(item) for item in node.elts} for node in ast.walk(tree)
            if isinstance(node, ast.Set) and node.elts and all(_string(item) for item in node.elts)]


def _families(trees):
    """Values each dynamic family can take, read from the literals that bound it."""
    admin = trees["admin_api.py"]
    patch = next(node for node in ast.walk(admin) if isinstance(node, ast.AsyncFunctionDef)
                 and node.name == "credentials_patch")
    fields = set().union(*_sets_of_strings(patch))
    run = next(node for node in ast.walk(trees["credential_actions.py"]) if isinstance(node, ast.FunctionDef)
               and node.name == "run")
    actions = max(_sets_of_strings(run), key=len)    # run()'s action allowlist
    log = next(node for node in ast.walk(trees["converter.py"]) if isinstance(node, ast.FunctionDef)
               and node.name == "_log")
    pattern = next(_string(node.args[0]) for node in ast.walk(log) if isinstance(node, ast.Call)
                   and isinstance(node.func, ast.Attribute) and node.func.attr == "match")
    components = re.search(r"\(([\w|]+)\)", pattern).group(1).split("|")
    return {
        ("admin_api.py", "credential."): {"credential." + field for field in fields},
        ("credential_actions.py", "credential."): {"credential." + action for action in actions},
        ("converter.py", None): set(components),   # Runtime events use the component as action.
        ("converter.py", "credit."): {"credit." + change for change in CREDIT_CHANGES},
    }


def emitted_actions():
    trees = {path.name: ast.parse(path.read_text(encoding="utf-8")) for path in SOURCES}
    families = _families(trees)
    actions, unknown = set(), []
    for name, tree in trees.items():
        helpers = HELPERS.get(name, {})
        skipped = {id(inner) for function in ast.walk(tree)
                   if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and (name, function.name) in FORWARDERS for inner in ast.walk(function)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or id(node) in skipped:
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "event" and len(node.args) >= 2:
                target, prefix = node.args[1], ""   # audit.event(kind, action, details)
            elif isinstance(func, ast.Name) and func.id in helpers:
                index, prefix = helpers[func.id]
                if len(node.args) <= index:
                    continue
                target = node.args[index]
            else:
                continue
            if (value := _string(target)) is not None:
                actions.add(prefix + value)
            elif isinstance(target, ast.BinOp) and _string(target.left) is not None:
                key = (name, _string(target.left))
                # buddy.audit_event() prefixes the stage itself; its stages are collected above.
                if key == ("buddy.py", "buddy."):
                    continue
                actions |= families.get(key) or unknown.append(f"{name}:{node.lineno}") or set()
            elif (name, None) in families:
                actions |= families[(name, None)]
            else:
                unknown.append(f"{name}:{node.lineno}")
    return actions, unknown


def labelled_actions():
    source = (ROOT / "web" / "src" / "values.tsx").read_text(encoding="utf-8")
    body = re.search(r"const eventActions: Record<string, MessageKey> = \{(.*?)\n\};", source, re.S).group(1)
    return set(re.findall(r'^\s*"?([\w.-]+)"?\s*:', body, re.M))


class AuditActionLabelTests(unittest.TestCase):
    def test_every_emitted_action_has_a_webui_label(self):
        actions, unknown = emitted_actions()
        self.assertEqual(unknown, [], "动态审计事件无法展开：请在本测试的 _families 中声明其取值来源")
        missing = sorted(actions - labelled_actions())
        self.assertEqual(missing, [], "以下审计事件缺少 WebUI 标签：请在 web/src/values.tsx 的 eventActions "
                                      "与 web/src/i18n.tsx 的中英文词条中补齐")

    def test_scan_sees_the_known_families(self):
        actions, _ = emitted_actions()
        for action in ("model.updated", "credential.auto_growth", "credential.reset-cooldown",
                       "buddy.departure", "buddy.task_chat", "housekeeper", "credit.expire"):
            self.assertIn(action, actions)


if __name__ == "__main__":
    unittest.main(verbosity=2)
