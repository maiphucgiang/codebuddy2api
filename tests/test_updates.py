#!/usr/bin/env python3
"""Release checks, self-update of release-archive installs, backups and the restart path.

Every network call goes through an httpx.MockTransport; nothing reaches GitHub.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Allow direct execution.

import errno
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx

from app import admin_account, startup, updates
from app.admin_api import install_admin
from app.backups import BackupError, Backups
from app.control_store import ControlStore
from app.gateway_management import install_pages
from app.settings import normalize_update_mirror
from app.updates import REPOSITORY_URL, UpdateError, Updater

from tests.admin_session import login as session_login

def tarball(files, *, prefix):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(f"{prefix}/{name}" if prefix else name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def release_files(version, requirements="pinned==1\n", schema=2):
    return {"VERSION": f"{version}\n", "converter.py": f"# gateway {version}\n",
            "app/__init__.py": "", "app/new_module.py": "NEW = True\n",
            "app/control_store.py": f"class ControlStore:\n    SCHEMA_VERSION = {schema}\n",
            "app/audit_store.py": "SCHEMA_VERSION = 4\n",
            "web/dist/index.html": f"<title>{version}</title>", "requirements.txt": requirements}


def fake_server():
    return SimpleNamespace(should_exit=False, config=SimpleNamespace(timeout_graceful_shutdown=None))


class UpdaterFixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        (self.root / "app").mkdir()
        (self.root / "app" / "old_module.py").write_text("OLD = True\n")
        (self.root / "app" / "__pycache__").mkdir()
        (self.root / "app" / "__pycache__" / "old.pyc").write_bytes(b"cache")
        (self.root / "web" / "dist").mkdir(parents=True)
        (self.root / "web" / "dist" / "index.html").write_text("<title>1.0.0</title>")
        (self.root / "converter.py").write_text("# gateway 1.0.0\n")
        (self.root / "VERSION").write_text("1.0.0\n")
        (self.root / "requirements.txt").write_text("pinned==1\n")
        (self.root / ".env").write_text("CODEBUDDY2API_KEY=keep\n")
        self.data = self.root / "auth"
        self.data.mkdir()
        db = sqlite3.connect(self.data / "control.sqlite3")
        db.execute("CREATE TABLE marker (value TEXT)")
        db.execute("INSERT INTO marker VALUES ('before-update')")
        db.commit()
        db.close()
        (self.data / "account.info").write_text('{"synthetic": true}')
        self.archive = tarball(release_files("1.1.0"), prefix="codebuddy2api-1.1.0")
        self.older = None     # A v0.9.0 archive, listed only when a test sets one.
        self.prefix = ""      # Mirror prefix the fake GitHub strips before routing.
        self.sums = None
        self.api_status = 200
        self.requests = []
        self.audit = Mock()
        self.config = {"server": fake_server(), "launch": {"argv": ["python3", "converter.py"], "env": {}, "cwd": "/"},
                       "audit_store": self.audit}

    def archives(self):
        return {"1.1.0": self.archive, **({"0.9.0": self.older} if self.older else {})}

    def releases(self):
        def assets(version, archive):
            return [{"name": f"codebuddy2api-{version}.tar.gz",
                     "digest": f"sha256:{hashlib.sha256(archive).hexdigest()}"}, {"name": "SHA256SUMS"}]
        listed = [
            {"tag_name": "v1.0.0", "name": "v1.0.0", "body": "old", "published_at": "2026-10-01T00:00:00Z",
             "assets": []},
            {"tag_name": "v1.1.0", "name": "v1.1.0", "body": "## Highlights\n- new", "html_url": "https://evil.example",
             "published_at": "2026-10-20T00:00:00Z", "assets": assets("1.1.0", self.archive)},
            {"tag_name": "v2.0.0", "draft": True, "assets": assets("1.1.0", self.archive)},
            {"tag_name": "v1.2.0-rc.1", "prerelease": True, "assets": assets("1.1.0", self.archive)},
            {"tag_name": "nightly", "assets": assets("1.1.0", self.archive)},
        ]
        if self.older:
            listed.append({"tag_name": "v0.9.0", "name": "v0.9.0", "body": "older",
                           "published_at": "2026-09-01T00:00:00Z", "assets": assets("0.9.0", self.older)})
        return listed

    def handler(self, request):
        self.requests.append(str(request.url))
        url = str(request.url)
        if url.startswith("https://api.github.com/"):
            if self.api_status != 200:
                return httpx.Response(self.api_status, json={"message": "rate limited"})
            return httpx.Response(200, json=self.releases())
        if not url.startswith(self.prefix):
            return httpx.Response(404)
        url = url[len(self.prefix):]
        for version, archive in self.archives().items():
            base = f"{REPOSITORY_URL}/releases/download/v{version}/"
            if url == base + "SHA256SUMS":
                sums = self.sums or f"{hashlib.sha256(archive).hexdigest()}  codebuddy2api-{version}.tar.gz\n"
                return httpx.Response(200, text=sums)
            if url == base + f"codebuddy2api-{version}.tar.gz":
                return httpx.Response(200, content=archive)
        return httpx.Response(404)

    def updater(self, deployment="archive"):
        return Updater(self.config, root=self.root, data_dir=self.data, version="1.0.0",
                       deployment=deployment, transport=httpx.MockTransport(self.handler))

    def wait(self, updater):
        deadline = time.monotonic() + 10
        while updater.view()["job"]["status"] == "running":
            if time.monotonic() > deadline:
                self.fail("update job did not finish")
            time.sleep(0.02)
        return updater.view()["job"]


class CheckTests(UpdaterFixture):
    def test_check_lists_stable_releases_newest_first_with_fixed_urls(self):
        view = self.updater().check()
        self.assertEqual([item["tag"] for item in view["releases"]], ["v1.1.0", "v1.0.0"])
        self.assertEqual(view["latest"], "1.1.0")
        self.assertTrue(view["update_available"])
        self.assertIsNone(view["error"])
        self.assertEqual(view["repository"], REPOSITORY_URL)
        newest = view["releases"][0]
        self.assertEqual(newest["url"], f"{REPOSITORY_URL}/releases/tag/v1.1.0")
        self.assertEqual(newest["notes"], "## Highlights\n- new")
        self.assertTrue(newest["installable"])
        self.assertFalse(view["releases"][1]["installable"])

    def test_checks_are_cached_and_manual_refresh_is_rate_limited(self):
        updater = self.updater()
        updater.check()
        updater.check()
        updater.check(force=True)
        self.assertEqual(len(self.requests), 1)
        updater._attempted_at -= updates.REFRESH_COOLDOWN + 1
        updater.check(force=True)
        self.assertEqual(len(self.requests), 2)

    def test_rate_limit_and_network_failures_keep_previous_releases(self):
        updater = self.updater()
        updater.check()
        self.api_status = 403
        updater._attempted_at = 0
        view = updater.check(force=True)
        self.assertIn("频率受限", view["error"])
        self.assertEqual(view["latest"], "1.1.0")
        offline = Updater(self.config, root=self.root, data_dir=self.data, version="1.0.0", deployment="archive",
                          transport=httpx.MockTransport(Mock(side_effect=httpx.ConnectError("offline"))))
        self.assertIn("无法连接 GitHub", offline.check()["error"])

    def test_every_deployment_self_updates_but_unmanaged_processes_cannot(self):
        for deployment in ("archive", "git", "docker"):
            self.assertEqual(self.updater(deployment).view()["self_update"], {"available": True, "reason": None})
        self.config.pop("server")
        view = self.updater().view()
        self.assertFalse(view["self_update"]["available"])
        self.assertIn("serve", view["self_update"]["reason"])
        with self.assertRaises(UpdateError):
            self.updater().install("v1.1.0")

    def test_deployment_detection_prefers_container_then_git(self):
        markers, real = {"/.dockerenv", "/run/.containerenv"}, updates.Path.exists
        with patch.object(updates.Path, "exists", lambda path: str(path) not in markers and real(path)):
            self.assertEqual(updates.detect_deployment(self.root), "archive")
            (self.root / ".git").mkdir()
            self.assertEqual(updates.detect_deployment(self.root), "git")
        with patch.object(updates.Path, "exists", return_value=True):
            self.assertEqual(updates.detect_deployment(self.root), "docker")


class InstallTests(UpdaterFixture):
    def test_install_backs_up_swaps_and_requests_restart(self):
        updater = self.updater()
        updater.check()
        updater.install("v1.1.0")
        job = self.wait(updater)
        self.assertEqual(job["status"], "restarting", job)
        self.assertEqual((job["step"], job["received"], job["total"]), ("restart", len(self.archive), len(self.archive)))
        self.assertEqual((self.root / "converter.py").read_text(), "# gateway 1.1.0\n")
        self.assertFalse((self.root / "app" / "old_module.py").exists())  # Directories are replaced whole.
        self.assertTrue((self.root / "app" / "new_module.py").exists())
        self.assertEqual((self.root / ".env").read_text(), "CODEBUDDY2API_KEY=keep\n")
        self.assertTrue((self.data / "account.info").exists())
        self.assertFalse((self.root / updates.WORK_DIR).exists())
        self.assertTrue(self.config["restart_requested"])
        self.assertTrue(self.config["server"].should_exit)
        self.assertEqual(self.config["server"].config.timeout_graceful_shutdown, 30)

        [backup] = updater.backups.list()
        self.assertEqual((backup["kind"], backup["from"], backup["to"], backup["name"]),
                         ("update", "1.0.0", "1.1.0", job["backup"]))
        with tarfile.open(updater.backups.path(backup["name"])) as tar:
            # Only the configuration and a record of when and between which versions: no
            # credentials, no program files.
            self.assertEqual(sorted(tar.getnames()), ["data/control.sqlite3", "manifest.json"])
            snapshot = tar.extractfile("data/control.sqlite3").read()
            manifest = json.loads(tar.extractfile("manifest.json").read())
        self.assertEqual((manifest["kind"], manifest["version"], manifest["target_version"]),
                         ("update", "1.0.0", "1.1.0"))
        self.assertEqual(manifest["files"], ["data/control.sqlite3"])
        stamp = time.strftime("%Y%m%d-%H%M%S", time.strptime(manifest["created_at"], "%Y-%m-%dT%H:%M:%SZ"))
        self.assertTrue(backup["name"].endswith(f"-{stamp}.tar.gz"))
        restored = self.root / "restored.sqlite3"
        restored.write_bytes(snapshot)
        with sqlite3.connect(restored) as db:
            self.assertEqual(db.execute("SELECT value FROM marker").fetchone(), ("before-update",))
        if os.name != "nt":
            self.assertEqual(updater.backups.path(backup["name"]).stat().st_mode & 0o777, 0o600)
        self.audit.event.assert_called_with("admin", "update.installed",
                                            {"from": "1.0.0", "to": "1.1.0", "file": backup["name"]})

    def assert_untouched(self):
        self.assertEqual((self.root / "converter.py").read_text(), "# gateway 1.0.0\n")
        self.assertTrue((self.root / "app" / "old_module.py").exists())
        self.assertFalse((self.root / updates.WORK_DIR).exists())
        self.assertNotIn("restart_requested", self.config)
        self.assertFalse(self.config["server"].should_exit)

    def test_checksum_mismatch_aborts_before_touching_anything(self):
        self.sums = "0" * 64 + "  codebuddy2api-1.1.0.tar.gz\n"
        updater = self.updater()
        updater.check()
        updater.install("v1.1.0")
        job = self.wait(updater)
        self.assertEqual(job["status"], "failed")
        self.assertIn("校验失败", job["message"])
        self.assertEqual(updater.backups.list(), [])
        self.assert_untouched()
        self.audit.event.assert_called_with("admin", "update.failed", {"to": "1.1.0", "code": "verify"})

    def test_archives_escaping_the_release_prefix_are_rejected(self):
        for files, prefix in (({"../escape.py": "x", **release_files("1.1.0")}, "codebuddy2api-1.1.0"),
                              (release_files("1.1.0"), "other-name")):
            with self.subTest(prefix=prefix):
                self.archive = tarball(files, prefix=prefix)
                updater = self.updater()
                updater.check()
                updater.install("v1.1.0")
                job = self.wait(updater)
                self.assertEqual(job["status"], "failed")
                self.assertTrue(any(word in job["message"] for word in ("结构异常", "不安全")), job["message"])
                self.assertFalse((self.root.parent / "escape.py").exists())
                self.assert_untouched()

    def test_archive_with_mismatched_version_is_rejected(self):
        self.archive = tarball(release_files("1.0.9"), prefix="codebuddy2api-1.1.0")
        updater = self.updater()
        updater.check()
        updater.install("v1.1.0")
        self.assertIn("版本不符", self.wait(updater)["message"])
        self.assert_untouched()

    def test_known_installable_other_versions_only_and_one_job_at_a_time(self):
        updater = self.updater()
        with self.assertRaisesRegex(UpdateError, "先检查更新"):
            updater.install("v1.1.0")
        updater.check()
        with self.assertRaisesRegex(UpdateError, "已经是该版本"):
            updater.install("v1.0.0")
        updater._job = {"status": "running"}
        with self.assertRaisesRegex(UpdateError, "正在进行"):
            updater.install("v1.1.0")

    def test_rollback_installs_an_older_release_through_the_same_flow(self):
        with sqlite3.connect(self.data / "control.sqlite3") as db:
            db.execute("PRAGMA user_version=2")
        self.older = tarball(release_files("0.9.0", schema=2), prefix="codebuddy2api-0.9.0")
        updater = self.updater()
        updater.check()
        updater.install("v0.9.0")
        self.assertEqual(self.wait(updater)["status"], "restarting")
        self.assertEqual((self.root / "VERSION").read_text(), "0.9.0\n")
        self.assertEqual([item["to"] for item in updater.backups.list()], ["0.9.0"])

    def test_rollback_stops_when_the_older_schema_cannot_read_the_data(self):
        with sqlite3.connect(self.data / "control.sqlite3") as db:
            db.execute("PRAGMA user_version=2")
        self.older = tarball(release_files("0.9.0", schema=1), prefix="codebuddy2api-0.9.0")
        updater = self.updater()
        updater.check()
        updater.install("v0.9.0")
        job = self.wait(updater)
        self.assertEqual(job["status"], "failed")
        self.assertIn("control.sqlite3（结构版本 2）", job["message"])
        self.assertEqual(updater.backups.list(), [])
        self.assert_untouched()

    def test_mirror_downloads_are_prefixed_and_still_bound_to_the_api_digest(self):
        self.config["update_mirror"] = self.prefix = "https://mirror.example/"
        updater = self.updater()
        updater.check()
        updater.install("v1.1.0")
        self.assertEqual(self.wait(updater)["status"], "restarting")
        downloads = [url for url in self.requests if "releases/download" in url]
        self.assertTrue(downloads and all(url.startswith("https://mirror.example/https://github.com/")
                                          for url in downloads))
        self.assertFalse(any(url.startswith("https://mirror.example/https://api.github.com") for url in self.requests))

    def test_a_mirror_serving_other_bytes_fails_verification(self):
        self.config["update_mirror"] = self.prefix = "https://mirror.example/"
        updater = self.updater()
        updater.check()
        # The mirror answers with a different archive and a checksum file that matches it.
        self.archive = tarball(release_files("1.1.0", requirements="evil==1\n"), prefix="codebuddy2api-1.1.0")
        updater.install("v1.1.0")
        self.assertIn("校验失败", self.wait(updater)["message"])
        self.assert_untouched()

    def test_container_layers_fall_back_to_copying_directories(self):
        updater = self.updater("docker")
        updater.check()
        real, owned = os.replace, {self.root / "app", self.root / "web"}

        def overlay(source, target):
            if Path(source) in owned:   # Directories from the image layer cannot be renamed.
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real(source, target)
        with patch.object(updates.os, "replace", side_effect=overlay):
            updater.install("v1.1.0")
            job = self.wait(updater)
        self.assertEqual(job["status"], "restarting", job)
        self.assertTrue((self.root / "app" / "new_module.py").exists())
        self.assertFalse((self.root / "app" / "old_module.py").exists())

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_git_checkouts_keep_untracked_and_ignored_files(self):
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        (self.root / ".gitignore").write_text("node_modules/\n__pycache__/\nauth/\n.env\n")
        for command in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "base"]):
            subprocess.run([*git, *command], cwd=self.root, check=True, capture_output=True)
        (self.root / "web" / "node_modules" / "pkg").mkdir(parents=True)
        (self.root / "web" / "node_modules" / "pkg" / "index.js").write_text("module")
        (self.root / "app" / "local_notes.md").write_text("mine")
        updater = self.updater("git")
        updater.check()
        updater.install("v1.1.0")
        self.assertEqual(self.wait(updater)["status"], "restarting")
        self.assertEqual((self.root / "web" / "node_modules" / "pkg" / "index.js").read_text(), "module")
        self.assertEqual((self.root / "app" / "local_notes.md").read_text(), "mine")
        self.assertTrue((self.root / "app" / "new_module.py").exists())
        self.assertFalse((self.root / "app" / "old_module.py").exists())   # Tracked files follow the release.
        self.assertFalse((self.root / "app" / "__pycache__").exists())
        self.assertTrue((self.root / ".git" / "HEAD").exists())

    def test_data_directory_inside_a_release_entry_is_refused(self):
        updater = Updater(self.config, root=self.root, data_dir=self.root / "app" / "data", version="1.0.0",
                          deployment="archive", transport=httpx.MockTransport(self.handler))
        updater.check()
        updater.install("v1.1.0")
        self.assertIn("数据目录", self.wait(updater)["message"])
        self.assert_untouched()

    def test_changed_requirements_are_installed_with_hashes_before_the_swap(self):
        self.archive = tarball(release_files("1.1.0", requirements="pinned==2\n"), prefix="codebuddy2api-1.1.0")
        updater = self.updater()
        updater.check()
        failed = SimpleNamespace(returncode=1, stdout="", stderr="resolver\nERROR: no matching wheel")
        with patch.object(updates.subprocess, "run", return_value=failed) as run:
            updater.install("v1.1.0")
            job = self.wait(updater)
        command = run.call_args.args[0]
        self.assertIn("--require-hashes", command)
        self.assertEqual(command[command.index("--only-binary") + 1], ":all:")
        self.assertEqual(job["status"], "failed")
        self.assertIn("no matching wheel", job["message"])
        self.assert_untouched()
        self.assertEqual(len(updater.backups.list()), 1)   # The backup was taken before dependencies.

    def test_swap_failure_rolls_every_entry_back(self):
        updater = self.updater()
        staged, previous = self.root / "staged", self.root / "previous"
        staged.mkdir()
        (staged / "converter.py").write_text("new")
        (staged / "VERSION").write_text("1.1.0")
        (staged / "added.txt").write_text("added")
        real = os.replace
        calls = []

        def flaky(source, target):
            calls.append(Path(target).name)
            if Path(source).name == "added.txt":
                raise OSError("disk full")
            return real(source, target)
        with patch.object(updates.os, "replace", side_effect=flaky), \
                self.assertRaisesRegex(UpdateError, "已回滚"):
            updater._swap(staged, previous, ["VERSION", "converter.py", "added.txt"])
        self.assertEqual((self.root / "converter.py").read_text(), "# gateway 1.0.0\n")
        self.assertEqual((self.root / "VERSION").read_text(), "1.0.0\n")
        self.assertFalse((self.root / "added.txt").exists())
        self.assertEqual((staged / "converter.py").read_text(), "new")


class MirrorTests(UpdaterFixture):
    def test_mirror_prefixes_are_normalized_https_only(self):
        self.assertEqual(normalize_update_mirror(" https://ghfast.top "), "https://ghfast.top/")
        self.assertEqual(normalize_update_mirror("https://example.com/gh-proxy/"), "https://example.com/gh-proxy/")
        self.assertEqual(normalize_update_mirror(""), "")
        for bad in ("http://ghfast.top/", "ghfast.top", "https://user:pw@ghfast.top/", "https://x.top/?a=1"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_update_mirror(bad)

    def test_probe_ranks_working_sources_and_rejects_pages_posing_as_files(self):
        def handler(request):
            url = str(request.url)
            if url.startswith("https://api.github.com/"):
                return httpx.Response(200, json=self.releases())
            if url.startswith("https://html.example/"):
                return httpx.Response(200, text="<html>mirror landing page</html>")
            if url.startswith("https://down.example/"):
                raise httpx.ConnectError("down")
            if url.startswith("https://limited.example/"):
                return httpx.Response(429)
            return httpx.Response(200, content=self.archive)
        self.config["update_mirror"] = "https://custom.example/"
        self.config["update_mirror_custom"] = "https://slot.example/"
        updater = Updater(self.config, root=self.root, data_dir=self.data, version="1.0.0", deployment="archive",
                          transport=httpx.MockTransport(handler))
        mirrors = ("https://html.example/", "https://down.example/", "https://limited.example/")
        with patch.object(updates, "MIRRORS", mirrors):
            results = {item["mirror"]: item for item in updater.test_mirrors()}
            [single] = updater.test_mirrors("https://limited.example/")
        self.assertEqual(set(results), {"", *mirrors, "https://custom.example/", "https://slot.example/"})
        self.assertEqual((single["mirror"], single["error"]), ("https://limited.example/", "HTTP 429"))
        self.assertTrue(results[""]["ok"] and results["https://custom.example/"]["ok"])
        self.assertGreater(results[""]["speed"], 0)
        self.assertIsNotNone(results[""]["latency_ms"])
        self.assertEqual(results["https://html.example/"]["error"], "返回的不是发布包")
        self.assertEqual(results["https://down.example/"]["error"], "无法连接")
        self.assertEqual(results["https://limited.example/"]["error"], "HTTP 429")


class BuildProvenanceTests(UpdaterFixture):
    def test_a_git_checkout_reports_whether_it_is_exactly_the_tagged_release(self):
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        (self.root / ".gitignore").write_text("auth/\n.env\n")
        for command in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "base"]):
            subprocess.run([*git, *command], cwd=self.root, check=True, capture_output=True)
        updater = self.updater("git")
        build = updater.build()
        self.assertEqual((build["tag_commit"], build["dirty"], build["exact"]), (None, False, False))
        subprocess.run([*git, "tag", "v1.0.0"], cwd=self.root, check=True, capture_output=True)
        self.assertTrue(updater.build()["exact"])
        (self.root / "converter.py").write_text("# edited\n")
        self.assertEqual((updater.build()["dirty"], updater.build()["exact"]), (True, False))
        subprocess.run([*git, "commit", "-qam", "ahead"], cwd=self.root, check=True, capture_output=True)
        build = updater.build()
        self.assertNotEqual(build["commit"], build["tag_commit"])
        self.assertEqual((build["dirty"], build["exact"]), (False, False))
        self.assertEqual(updater.view()["build"]["commit"], build["commit"])

    def test_an_image_reports_its_channel_only_while_it_runs_its_own_version(self):
        updater = self.updater("docker")
        self.assertIsNone(updater.build())
        (self.root / "build.json").write_text('{"channel": "edge", "commit": "0123456789abcdef", "version": "1.0.0"}')
        self.assertEqual(updater.build(), {"channel": "edge", "commit": "0123456789ab"})
        # After a one-click update the program is a release while the file still describes the image.
        (self.root / "build.json").write_text('{"channel": "edge", "commit": "0123456789abcdef", "version": "0.9.0"}')
        self.assertIsNone(updater.build())


class BackupTests(UpdaterFixture):
    def backups(self, **settings):
        self.config.update(settings)
        return Backups(self.config, data_dir=self.data, version="1.0.0")

    def test_backups_are_listed_and_deleted_by_strict_name_only(self):
        backups = self.backups()
        backups.directory.mkdir()
        update = "update-1.0.0-to-1.1.0-20261020-120000.tar.gz"
        manual = "manual-1.1.0-20261021-080000.tar.gz"
        (backups.directory / update).write_bytes(b"x" * 10)
        (backups.directory / manual).write_bytes(b"y" * 4)
        (backups.directory / "update-1.0.0-to-1.1.0-20261019-120000.tar.gz.tmp").write_bytes(b"partial")
        (backups.directory / "notes.txt").write_text("ignored")
        self.assertEqual(backups.list(), [
            {"name": manual, "kind": "manual", "version": "1.1.0", "size": 4, "created_at": 1792569600},
            {"name": update, "kind": "update", "version": "1.0.0", "from": "1.0.0", "to": "1.1.0",
             "size": 10, "created_at": 1792497600},
        ])
        for bad in ("../control.sqlite3", "notes.txt", "update-1.0.0-to-1.1.0-20261019-120000.tar.gz.tmp"):
            with self.assertRaises(BackupError):
                backups.delete(bad)
        backups.delete(update)
        backups.delete(manual)
        self.assertEqual(backups.list(), [])

    def test_a_manual_backup_holds_the_configuration_and_its_record_only(self):
        backups = self.backups()
        name = backups.create("manual")
        self.assertRegex(name, r"^manual-1\.0\.0-\d{8}-\d{6}\.tar\.gz$")
        with tarfile.open(backups.path(name)) as tar:
            self.assertEqual(sorted(tar.getnames()), ["data/control.sqlite3", "manifest.json"])
            manifest = json.loads(tar.extractfile("manifest.json").read())
        self.assertEqual((manifest["kind"], manifest["version"]), ("manual", "1.0.0"))
        self.assertNotIn("target_version", manifest)
        # A second backup within the same second would take the same name.
        with patch("app.backups.time.time", return_value=float(backups.list()[0]["created_at"])), \
                self.assertRaises(BackupError):
            backups.create("manual")
        self.assertFalse(any(entry.name.startswith(".tmp-") for entry in backups.directory.iterdir()))

    def test_scheduled_backups_run_when_due_and_prune_only_their_own(self):
        backups = self.backups(backup_auto=False, backup_interval_hours=24, backup_keep=2)
        self.assertIsNone(backups.run_due())
        self.assertIsNone(backups.schedule()["next_at"])
        backups.directory.mkdir()
        now = time.time()
        for hours in (100, 75, 50):
            stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now - hours * 3600))
            (backups.directory / f"auto-1.0.0-{stamp}.tar.gz").write_bytes(b"auto")
        (backups.directory / "manual-1.0.0-20200101-000000.tar.gz").write_bytes(b"manual")
        self.config["backup_auto"] = True
        created = backups.run_due()   # The newest scheduled backup is 50 hours old.
        kinds = [item["kind"] for item in backups.list()]
        self.assertEqual((kinds.count("auto"), kinds.count("manual")), (2, 1))
        self.assertEqual(backups.list()[0]["name"], created)
        schedule = backups.schedule()
        self.assertEqual((schedule["enabled"], schedule["keep"], schedule["last_at"]),
                         (True, 2, backups.list()[0]["created_at"]))
        self.assertAlmostEqual(schedule["next_at"], schedule["last_at"] + 24 * 3600, delta=1)
        self.assertIsNone(backups.run_due())   # Not due again for a day.


class RestartTests(unittest.TestCase):
    def test_restart_requires_a_served_process(self):
        with self.assertRaises(RuntimeError):
            startup.request_restart({})
        config = {"server": fake_server(), "launch": {"argv": ["python3"], "env": {}, "cwd": "/"}}
        startup.request_restart(config, grace=5)
        self.assertTrue(config["restart_requested"])
        self.assertTrue(config["server"].should_exit)
        self.assertEqual(config["server"].config.timeout_graceful_shutdown, 5)

    def test_restart_reexecutes_the_original_invocation_and_environment(self):
        with patch.dict(os.environ, {"FROM_DOTENV": "loaded-later"}):
            launch = startup.capture_launch()
        self.assertEqual(launch["argv"], list(sys.orig_argv))
        self.assertEqual(launch["env"]["FROM_DOTENV"], "loaded-later")
        launch = {"argv": ["python3", "-B", "converter.py", "--port", "9000", "--desensitize"],
                  "env": {"ONLY": "original"}, "cwd": "/srv/app"}
        with patch.object(startup.os, "execve") as execve, patch.object(startup.os, "chdir") as chdir, \
                patch.object(startup.os, "name", "posix"):
            startup.restart_process(launch)
        chdir.assert_called_once_with("/srv/app")
        execve.assert_called_once_with(sys.executable, launch["argv"], {"ONLY": "original"})

    def test_run_server_exposes_the_server_while_serving(self):
        import uvicorn
        config, seen = {}, {}

        def run(server):
            seen["server"] = config.get("server")
            server.started = True
        with patch.object(uvicorn.Server, "run", run), patch.object(startup, "_install_lease_release"):
            startup.run_server(FastAPI(), config, host="127.0.0.1", port=8787)
        self.assertIsInstance(seen["server"], uvicorn.Server)
        self.assertNotIn("server", config)


class AdminRouteTests(UpdaterFixture):
    def setUp(self):
        super().setUp()
        store = ControlStore(self.root / "control-admin.sqlite3")
        self.addCleanup(store.close)
        self.audit.storage.return_value = {"db_bytes": 0}
        self.audit.path = None
        self.config.update(api_key="synthetic-key", control_store=store,
                           session_path=self.root / "admin-sessions.json")
        self.config["updater"] = self.updater()
        self.config["backups"] = self.config["updater"].backups
        admin_account.save(store, admin_account.new("admin", "synthetic-key", "user"))
        app = FastAPI()
        install_admin(app, self.config, SimpleNamespace())
        self.client = self.enterContext(TestClient(app))
        self.headers = session_login(self.client)

    def test_update_routes_report_state_and_map_failures_to_messages(self):
        view = self.client.get("/admin/update", headers=self.headers).json()
        self.assertEqual((view["current"], view["deployment"], view["releases"]), ("1.0.0", "archive", []))
        checked = self.client.post("/admin/update/check", headers=self.headers).json()
        self.assertEqual(checked["latest"], "1.1.0")
        refused = self.client.post("/admin/update/install", json={"tag": "v1.0.0"}, headers=self.headers)
        self.assertEqual(refused.status_code, 422)
        self.assertIn("已经是该版本", refused.json()["error"]["message"])
        self.assertEqual(self.client.post("/admin/update/install", json={"tag": 1},
                                          headers=self.headers).status_code, 400)
        started = self.client.post("/admin/update/install", json={"tag": "v1.1.0"}, headers=self.headers)
        self.assertEqual(started.status_code, 202)
        self.assertEqual(self.wait(self.config["updater"])["status"], "restarting")
        self.audit.event.assert_any_call("admin", "update.started", {"to": "v1.1.0"})

    def test_storage_creates_lists_downloads_and_deletes_backups(self):
        directory = self.config["backups"].directory
        directory.mkdir()
        name = "update-1.0.0-to-1.1.0-20261020-120000.tar.gz"
        (directory / name).write_bytes(b"backup-bytes")
        storage = self.client.get("/admin/storage", headers=self.headers).json()
        self.assertEqual([item["name"] for item in storage["backups"]], [name])
        download = self.client.get(f"/admin/storage/backups/{name}", headers=self.headers)
        self.assertEqual((download.status_code, download.content), (200, b"backup-bytes"))
        self.assertIn(name, download.headers["content-disposition"])
        self.assertEqual(self.client.get("/admin/storage/backups/control.sqlite3",
                                         headers=self.headers).status_code, 404)
        deleted = self.client.delete(f"/admin/storage/backups/{name}", headers=self.headers)
        self.assertEqual(deleted.json(), {"deleted": name})
        self.assertEqual(self.client.delete(f"/admin/storage/backups/{name}", headers=self.headers).status_code, 404)
        self.audit.event.assert_any_call("admin", "storage.backup_deleted", {"file": name})
        created = self.client.post("/admin/storage/backups", headers=self.headers).json()["created"]
        self.assertTrue(created.startswith("manual-1.0.0-"))
        storage = self.client.get("/admin/storage", headers=self.headers).json()
        self.assertEqual([item["kind"] for item in storage["backups"]], ["manual"])
        self.assertEqual(storage["backup_schedule"]["enabled"], False)
        self.audit.event.assert_any_call("admin", "storage.backup_created", {"file": created})

    def test_mirror_route_saves_validates_and_respects_locks(self):
        saved = self.client.post("/admin/update/mirror", json={"mirror": "https://ghfast.top"}, headers=self.headers)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["mirror"], "https://ghfast.top/")
        self.assertEqual(self.config["control_store"].snapshot()["settings"]["update_mirror"], "https://ghfast.top/")
        invalid = self.client.post("/admin/update/mirror", json={"mirror": "http://ghfast.top/"}, headers=self.headers)
        self.assertEqual(invalid.status_code, 422)
        self.assertIn("https", invalid.json()["error"]["message"])
        self.config["settings_sources"]["update_mirror"] = "environment"
        locked = self.client.post("/admin/update/mirror", json={"mirror": ""}, headers=self.headers)
        self.assertEqual(locked.status_code, 422)
        self.assertIn("锁定", locked.json()["error"]["message"])
        self.assertTrue(self.client.get("/admin/update", headers=self.headers).json()["mirror_locked"])

    def test_mirror_route_remembers_the_custom_slot(self):
        saved = self.client.post("/admin/update/mirror", json={"mirror": "https://slot.example",
                                                               "custom": "https://slot.example"}, headers=self.headers)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["mirror_custom"], "https://slot.example/")
        switched = self.client.post("/admin/update/mirror", json={"mirror": ""}, headers=self.headers)
        self.assertEqual((switched.json()["mirror"], switched.json()["mirror_custom"]), ("", "https://slot.example/"))
        settings = self.config["control_store"].snapshot()["settings"]
        self.assertEqual(settings["update_mirror_custom"], "https://slot.example/")
        invalid = self.client.post("/admin/update/mirror", json={"mirror": "", "custom": "ftp://slot"},
                                   headers=self.headers)
        self.assertEqual(invalid.status_code, 422)

    def test_mirror_test_route_probes_one_source_or_all(self):
        with patch.object(self.config["updater"], "test_mirrors", return_value=[]) as probe:
            self.assertEqual(self.client.post("/admin/update/mirrors/test", headers=self.headers).status_code, 200)
            self.client.post("/admin/update/mirrors/test", json={"mirror": "https://ghfast.top"}, headers=self.headers)
            invalid = self.client.post("/admin/update/mirrors/test", json={"mirror": "http://x"}, headers=self.headers)
        self.assertEqual([call.args for call in probe.call_args_list], [(None,), ("https://ghfast.top/",)])
        self.assertEqual(invalid.status_code, 422)


class PageRouteTests(unittest.TestCase):
    def test_storage_updates_and_release_pages_load_directly(self):
        dist = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (dist / "index.html").write_text("<div id=root></div>")
        app = FastAPI()
        install_pages(app, dist)
        client = self.enterContext(TestClient(app, follow_redirects=False))
        for page in ("storage", "updates", "updates/v1.4.0"):
            with self.subTest(page=page):
                self.assertEqual(client.get(f"/dashboard/{page}").status_code, 200)
        self.assertEqual(client.get("/dashboard/updates/v1.4").status_code, 404)
        self.assertEqual(client.get("/dashboard/updates/latest").status_code, 302)


if __name__ == "__main__":
    unittest.main(verbosity=2)
