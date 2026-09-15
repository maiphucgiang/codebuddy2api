"""部署模板、Docker 运行文件与 Python 3.12 语法的离线回归。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 仓库根：允许直接运行本文件

import ast
import pathlib
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from textwrap import dedent


ROOT = Path(__file__).resolve().parents[1]  # 仓库根
RUNTIME_DEFAULTS = {
    "max_images": 16, "image_policy": "truncate",
    "max_request_bytes": 33554432, "log_body_limit": 65536,
    "admin_csrf": True, "keep_tool_metadata": False,
}
API_ENDPOINTS = {
    "chat/completions": "POST", "responses": "POST", "messages": "POST",
    "messages/count_tokens": "POST", "models": "GET",
}


def docker_sources():
    """Inspect local runtime-stage COPY sources, excluding frontend build artifacts."""
    files = set()
    runtime = False
    for line in (ROOT / "Dockerfile").read_text().splitlines():
        parts = shlex.split(line)
        if parts and parts[0] == "FROM":
            runtime = parts[-1] == "runtime" or parts[1].startswith("python:")
        if not runtime or not parts or parts[0] != "COPY" or any(p.startswith("--from=") for p in parts):
            continue
        for source in parts[1:-1]:
            path = ROOT / source
            if source.endswith("/"):
                for pattern in ("*.py", "adapters/*.py"):
                    files.update(str(item.relative_to(ROOT)) for item in path.glob(pattern))
            elif path.is_dir():
                files.update(str(item.relative_to(ROOT)) for item in path.rglob("*.py"))
            else:
                files.add(source)
    return files


def _local_dependency(source: str, module: str) -> str | None:
    """把一条 import 映射到仓库内的运行时文件；外部依赖返回 None。"""
    if module.startswith("."):  # 包内相对导入：相对当前文件所在目录解析
        base = pathlib.PurePosixPath(source).parent
        candidate = (base / module.lstrip(".").replace(".", "/")).with_suffix(".py")
        return str(candidate) if (ROOT / str(candidate)).is_file() else None
    if not module.startswith("app."):  # 仅校验仓库内运行时模块
        return None
    candidate = pathlib.PurePosixPath(module.replace(".", "/")).with_suffix(".py")
    if (ROOT / str(candidate)).is_file():
        return str(candidate)
    package = pathlib.PurePosixPath(module.replace(".", "/")) / "__init__.py"
    return str(package) if (ROOT / str(package)).is_file() else None


class DeploymentTests(unittest.TestCase):
    def test_env_template_matches_runtime_defaults_and_contains_no_shared_key(self):
        values = {}
        for line in (ROOT / ".env.example").read_text().splitlines():
            if line and not line.startswith("#"):
                key, value = line.split("=", 1)
                values[key] = value
        for env_key, config_key in (("MAX_IMAGES", "max_images"), ("IMAGE_POLICY", "image_policy"),
                                    ("MAX_REQUEST_BYTES", "max_request_bytes"), ("LOG_BODY_LIMIT", "log_body_limit")):
            self.assertEqual(values["CODEBUDDY2API_" + env_key], str(RUNTIME_DEFAULTS[config_key]))
        self.assertEqual(values["CODEBUDDY2API_KEY"], "")
        self.assertEqual(values["CODEBUDDY2API_BIND"], "127.0.0.1")
        self.assertEqual(values["CODEBUDDY2API_IMAGE"], "codebuddy2api:local")
        self.assertEqual(values["CODEBUDDY2API_AUTO_TRIAL"], "false")
        self.assertEqual(values["CODEBUDDY2API_ADMIN_CSRF"], str(RUNTIME_DEFAULTS["admin_csrf"]).lower())
        self.assertNotIn("CODEBUDDY2API_KEEP_TOOL_METADATA", values)

    def test_tool_metadata_compose_environment_is_optional(self):
        key = "CODEBUDDY2API_KEEP_TOOL_METADATA"
        self.assertRegex((ROOT / "docker-compose.yml").read_text(), rf"(?m)^ +{key}: *$")
        self.assertRegex((ROOT / ".env.example").read_text(), rf"(?m)^# {key}=(true|false)$")

    def test_docker_copies_and_allows_all_local_runtime_imports(self):
        files = docker_sources()
        self.assertTrue({"app/client_profiles.py", "app/site_routing.py", "app/trial_rewards.py"} <= files)
        self.assertTrue({"app/adapters/anthropic_adapter.py", "app/adapters/responses_adapter.py",
                         "app/adapters/responses_projection.py"} <= files)
        ignore = (ROOT / ".dockerignore").read_text().splitlines()
        self.assertEqual(ignore[1], "**")
        for filename in files:
            self.assertTrue((ROOT / filename).is_file(), filename)
            if not filename.endswith(".py"):
                continue
            self.assertTrue(any(line == "!" + filename or pathlib.PurePosixPath(filename).match(line[1:])
                                for line in ignore if line.startswith("!")), filename)
            tree = ast.parse((ROOT / filename).read_text(), filename, feature_version=(3, 12))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [f"{'.' * node.level}{node.module or ''}"]
                else:
                    continue
                for module in modules:
                    dependency = _local_dependency(filename, module)
                    if dependency is not None:
                        self.assertIn(dependency, files, f"{filename} imports missing {dependency}")
        self.assertNotIn(".env", files)
        self.assertNotIn(".env.example", files)

    def test_docker_runtime_file_set_imports_in_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            for filename in docker_sources():
                target = Path(directory) / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / filename, target)
            # 不在仓库内导入运行时；临时 HOME、净环境及审计钩子阻断联网和凭据访问。
            command = dedent("""\
                import os
                import sys
                from pathlib import Path

                def deny_external_access(event, args):
                    if event.startswith("socket.") or event in {"subprocess.Popen", "os.system"}:
                        raise RuntimeError("External access is forbidden in deployment tests")
                    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
                        path = Path(os.fsdecode(args[0]))
                        if (path.name == ".env" or path.suffix == ".info"
                                or "auth" in path.parts or ".codebuddy" in path.parts):
                            raise RuntimeError("Credential access is forbidden in deployment tests")

                sys.addaudithook(deny_external_access)
                import converter
                from app import client_profiles
                from app import site_routing

                assert converter.health() == {"status": "ok"}
                routes = {(route.path, method) for route in converter.app.routes
                          for method in getattr(route, "methods", ())}
                """)
            command += f"\nexpected_defaults = {RUNTIME_DEFAULTS!r}\n"
            command += "assert {key: converter.CONFIG[key] for key in expected_defaults} == expected_defaults\n"
            command += f"endpoints = {API_ENDPOINTS!r}\n"
            command += dedent("""\
                for suffix, method in endpoints.items():
                    assert ("/v1/" + suffix, method) in routes
                    for prefix in ("/cn/v1", "/intl/v1"):
                        assert (prefix + "/" + suffix, method) not in routes
                """)
            result = subprocess.run([sys.executable, "-B", "-c", command], cwd=directory,
                                    env={"PATH": os.defpath, "PYTHONPATH": directory, "HOME": directory,
                                         "XDG_CONFIG_HOME": directory, "XDG_CACHE_HOME": directory,
                                         "CODEBUDDY_AUTH_DIR": str(Path(directory) / "auth")},
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_compose_has_old_syntax_fallbacks_without_required_env_file(self):
        text = (ROOT / "docker-compose.yml").read_text()
        self.assertIn('version: "3.8"', text)
        self.assertNotIn("env_file:", text)
        self.assertNotIn("required:", text)
        for name, fallback in (("MAX_IMAGES", "16"), ("IMAGE_POLICY", "truncate"),
                               ("MAX_REQUEST_BYTES", "33554432"), ("LOG_BODY_LIMIT", "65536"), ("ADMIN_CSRF", "true")):
            self.assertIn("${CODEBUDDY2API_" + name + ":-" + fallback + "}", text)
        for filename in ("README.md", "README.zh-CN.md"):
            doc = (ROOT / filename).read_text()
            self.assertIn("cp .env.example .env", doc)
            self.assertIn("docker compose pull", doc)
            self.assertIn("docker compose up -d --no-build", doc)
            self.assertIn("ghcr.io/maiphucgiang/codebuddy2api:latest", doc)
            self.assertNotIn("docker compose build", doc)
            self.assertNotRegex(doc, r"\bdocker-compose\s")

    def test_readmes_link_guides_and_webui_setup(self):
        for suffix in ("", ".zh-CN"):
            with self.subTest(language=suffix):
                readme = (ROOT / f"README{suffix}.md").read_text()
                self.assertIn("http://127.0.0.1:8787/dashboard", readme)
                self.assertIn("CODEBUDDY2API_KEY", readme)
                for guide in ("webui", "deployment", "clients", "advanced"):
                    target = f"docs/{guide}{suffix}.md"
                    self.assertIn(f"]({target})", readme)
                    self.assertTrue((ROOT / target).is_file(), target)

    def test_docs_keep_original_endpoints_and_sdk_roots_without_region_prefixes(self):
        for suffix in ("", ".zh-CN"):
            with self.subTest(language=suffix):
                readme = (ROOT / f"README{suffix}.md").read_text()
                clients = (ROOT / f"docs/clients{suffix}.md").read_text()
                advanced = (ROOT / f"docs/advanced{suffix}.md").read_text()
                for endpoint, method in API_ENDPOINTS.items():
                    self.assertIn(f"{method} /v1/{endpoint}", advanced)
                self.assertIn("`http://127.0.0.1:8787/v1`", readme)
                self.assertIn('base_url = "http://127.0.0.1:8787/v1"', clients)
                for region in ("cn", "intl"):
                    self.assertNotIn(f"http://127.0.0.1:8787/{region}", readme + clients + advanced)
                    for product in ("cli", "work"):
                        self.assertIn(f"`{region}-{product}`", advanced)
                self.assertIn("ANTHROPIC_BASE_URL=http://127.0.0.1:8787\n", clients)
                self.assertNotIn("ANTHROPIC_BASE_URL=http://127.0.0.1:8787/v1", clients)
                for host in ("copilot.tencent.com", "www.workbuddy.cn", "www.codebuddy.ai", "www.workbuddy.ai"):
                    self.assertIn("https://" + host, advanced)
                self.assertIn("default-model", advanced)
                self.assertIn("账号/租户" if suffix else "account/tenant", advanced)

    def test_documentation_local_links_resolve(self):
        paths = [ROOT / "README.md", ROOT / "README.zh-CN.md", *sorted((ROOT / "docs").glob("*.md"))]
        for path in paths:
            for target in re.findall(r"\[[^\]]+\]\(([^\s)]+)\)", path.read_text()):
                if "://" in target or target.startswith(("mailto:", "#")):
                    continue
                linked = (path.parent / target.split("#", 1)[0]).resolve()
                with self.subTest(document=path.relative_to(ROOT), target=target):
                    self.assertTrue(linked.is_relative_to(ROOT), target)
                    self.assertTrue(linked.is_file(), target)


if __name__ == "__main__":
    unittest.main(verbosity=2)
