"""部署模板、Docker 运行文件与 Python 3.12 语法的离线回归。"""

import ast
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from textwrap import dedent


ROOT = Path(__file__).resolve().parent
RUNTIME_DEFAULTS = {
    "max_images": 16, "image_policy": "truncate",
    "max_request_bytes": 33554432, "log_body_limit": 65536,
}
API_ENDPOINTS = {
    "chat/completions": "POST", "responses": "POST", "messages": "POST",
    "messages/count_tokens": "POST", "models": "GET",
}


def docker_sources():
    files = set()
    for line in (ROOT / "Dockerfile").read_text().splitlines():
        parts = shlex.split(line)
        if parts and parts[0] == "COPY":
            files.update(parts[1:-1])
    return files


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

    def test_docker_copies_and_allows_all_local_runtime_imports(self):
        files = docker_sources()
        self.assertTrue({"client_profiles.py", "site_routing.py", "trial_rewards.py"} <= files)
        ignore = (ROOT / ".dockerignore").read_text().splitlines()
        self.assertEqual(ignore[1], "**")
        for filename in files:
            self.assertTrue((ROOT / filename).is_file(), filename)
            self.assertIn("!" + filename, ignore)
            if not filename.endswith(".py"):
                continue
            tree = ast.parse((ROOT / filename).read_text(), filename, feature_version=(3, 12))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module] if node.module else []
                else:
                    continue
                for module in modules:
                    dependency = module.split(".")[0] + ".py"
                    if (ROOT / dependency).is_file():
                        self.assertIn(dependency, files, f"{filename} imports missing {dependency}")
        self.assertNotIn(".env", files)
        self.assertNotIn(".env.example", files)

    def test_docker_runtime_file_set_imports_in_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            for filename in docker_sources():
                shutil.copyfile(ROOT / filename, Path(directory) / filename)
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
                import client_profiles
                import site_routing

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
                               ("MAX_REQUEST_BYTES", "33554432"), ("LOG_BODY_LIMIT", "65536")):
            self.assertIn("${CODEBUDDY2API_" + name + ":-" + fallback + "}", text)
        for filename in ("README.md", "README.zh-CN.md"):
            doc = (ROOT / filename).read_text()
            self.assertIn("cp .env.example .env", doc)
            self.assertIn("docker compose build", doc)
            self.assertNotRegex(doc, r"\bdocker-compose\s")

    def test_readmes_keep_original_endpoints_and_sdk_roots_without_region_prefixes(self):
        for filename in ("README.md", "README.zh-CN.md"):
            with self.subTest(filename=filename):
                doc = (ROOT / filename).read_text()
                for suffix, method in API_ENDPOINTS.items():
                    self.assertIn(f"{method} /v1/{suffix}", doc)
                self.assertIn("`http://127.0.0.1:8787/v1`", doc)
                self.assertIn('base_url = "http://127.0.0.1:8787/v1"', doc)
                for region in ("cn", "intl"):
                    self.assertNotIn(f"http://127.0.0.1:8787/{region}", doc)
                    for product in ("cli", "work"):
                        self.assertIn(f"`{region}-{product}`", doc)
                self.assertIn("ANTHROPIC_BASE_URL=http://127.0.0.1:8787\n", doc)
                self.assertNotIn("ANTHROPIC_BASE_URL=http://127.0.0.1:8787/v1", doc)
                for host in ("copilot.tencent.com", "www.workbuddy.cn", "www.codebuddy.ai", "www.workbuddy.ai"):
                    self.assertIn("https://" + host, doc)
                self.assertIn("default-model", doc)
                self.assertIn("account/tenant" if filename == "README.md" else "账号/租户", doc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
