"""Offline contracts for dependency hashes and immutable Docker inputs."""

from pathlib import Path
import re
import tomllib
import shlex
import unittest

import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parents[1]


def requirement_lines(path):
    for line in path.read_text().replace("\\\n", " ").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            yield line.strip()


class BuildLockTests(unittest.TestCase):
    def test_every_resolved_dependency_has_an_exact_version_and_sha256(self):
        locked = set()
        for line in requirement_lines(ROOT / "requirements.txt"):
            requirement, *hashes = re.split(r"\s+--hash=", line)
            parsed = Requirement(requirement)
            with self.subTest(package=parsed.name):
                self.assertIsNone(parsed.url)
                pins = list(parsed.specifier)
                self.assertEqual(len(pins), 1)
                self.assertEqual(pins[0].operator, "==")
                self.assertNotIn("*", pins[0].version)
                self.assertTrue(hashes)
                for digest in hashes:
                    self.assertRegex(digest.strip(), r"^sha256:[0-9a-f]{64}$")
                locked.add(canonicalize_name(parsed.name))
        direct = {canonicalize_name(Requirement(line).name)
                  for line in requirement_lines(ROOT / "requirements.in")}
        self.assertTrue(direct)
        self.assertTrue(direct <= locked)

    def test_uv_metadata_and_exported_locks_stay_consistent(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        self.assertEqual(project["version"], (ROOT / "VERSION").read_text().strip())
        self.assertEqual(set(project["dependencies"]), set(requirement_lines(ROOT / "requirements.in")))
        lock = tomllib.loads((ROOT / "uv.lock").read_text())
        packages = {p["name"]: p for p in lock["package"] if p["name"] != project["name"]}
        exported = set()
        for line in requirement_lines(ROOT / "requirements.txt"):
            requirement, *hashes = re.split(r"\s+--hash=", line)
            parsed = Requirement(requirement)
            package = packages[canonicalize_name(parsed.name)]
            self.assertIn(package["version"], parsed.specifier)
            locked_hashes = {wheel["hash"] for wheel in package.get("wheels", [])}
            if package.get("sdist"):
                locked_hashes.add(package["sdist"]["hash"])
            self.assertTrue({value.strip() for value in hashes} <= locked_hashes)
            exported.add(canonicalize_name(parsed.name))
        self.assertEqual(exported, set(packages))

    def test_external_images_and_build_frontend_are_pinned(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertRegex(dockerfile.splitlines()[0], r"^# syntax=docker/dockerfile:[^@]+@sha256:[0-9a-f]{64}$")
        stages = set()
        external = []
        for line in dockerfile.splitlines():
            parts = shlex.split(line)
            if not parts or parts[0] != "FROM":
                continue
            image = next(part for part in parts[1:] if not part.startswith("--"))
            if "${BUILDARCH}" in image:
                for arch in ("amd64", "arm64"):
                    self.assertIn(image.replace("${BUILDARCH}", arch), stages)
            elif image != "scratch" and image not in stages:
                self.assertRegex(image, r"@sha256:[0-9a-f]{64}$")
                external.append(image)
            if "AS" in parts:
                stages.add(parts[parts.index("AS") + 1])
        self.assertTrue(any(image.startswith("node:") for image in external))
        self.assertTrue(any(image.startswith("python:") for image in external))

    def test_ci_workflow_parses_and_keeps_install_flags_in_the_run_string(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/docker.yml").read_text())
        for job in ("test", "web"):
            install = next(step["run"] for step in workflow["jobs"][job]["steps"]
                           if "-r requirements.txt" in step.get("run", ""))
            self.assertEqual(shlex.split(install), ["python", "-m", "pip", "install",
                             "--require-hashes", "--only-binary=:all:", "-r", "requirements.txt"])


    def test_ci_and_container_installs_require_verified_wheels(self):
        installs = []
        for path in (ROOT / "Dockerfile", ROOT / ".github/workflows/docker.yml"):
            for line in path.read_text().splitlines():
                if "install" in line and "-r requirements.txt" in line:
                    installs.append(line)
                    self.assertIn("--require-hashes", line)
                    self.assertIn("--only-binary=:all:", line)
        self.assertGreaterEqual(len(installs), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
