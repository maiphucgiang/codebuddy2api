"""Release checks, in-place install of any release archive, and update backups.

Releases are listed from the GitHub API. Installing one, newer or older, downloads its
archive (optionally through a download mirror), verifies it against the published
checksums and the digest the API reports, backs up the control database,
swaps the release entries in with same-volume renames (rolled back on failure) and
restarts the process in place with its original command line. Release archives, git
checkouts and containers take the same path; git checkouts keep their untracked and
ignored files, and container image layers fall back to copying what they cannot rename.
"""
from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import threading
import time

import httpx

from .backups import BackupError, Backups
from .startup import request_restart

REPOSITORY = "maiphucgiang/codebuddy2api"
REPOSITORY_URL = f"https://github.com/{REPOSITORY}"
RELEASES_API = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=20"
CHECK_INTERVAL = 6 * 3600
REFRESH_COOLDOWN = 60          # Manual checks share the anonymous GitHub API quota (60/hour).
WORK_DIR = ".update"           # Staging lives under the install root so renames stay on one volume.
MAX_API_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_UNPACKED_BYTES = 256 * 1024 * 1024
MAX_MEMBERS = 20000
MAX_NOTES = 64 * 1024
TAG = re.compile(r"v((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))")
# Public prefix proxies for github.com downloads. None of them proxies api.github.com, so
# Public prefix proxies for github.com downloads. None of them proxies api.github.com, so
# releases are always listed directly, and mirrored bytes are only trusted once they match
# the digest the GitHub API reports.
MIRRORS = ("https://ghfast.top/", "https://gh-proxy.com/", "https://ghproxy.net/", "https://gh-proxy.org/",
           "https://gh.ddlc.top/", "https://ghproxy.cc/", "https://github.akams.cn/", "https://hub.gitmirror.com/")
PROBE_BYTES = 512 * 1024
PROBE_SECONDS = 8
# Written into images at build time with the channel (edge for main, stable for tags) and commit.
BUILD_FILE = "build.json"
# Databases an older release must still open after a rollback, and where it declares their schema.
SCHEMAS = (("control.sqlite3", "app/control_store.py"), ("logs.sqlite3", "app/audit_store.py"))


class UpdateError(Exception):
    """A user-facing update failure; the message is shown in the WebUI as is."""


def _version(text):
    return tuple(int(part) for part in text.split("."))


def _declared_schema(path):
    """The SCHEMA_VERSION literal a release declares, read without importing its code."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                and type(node.value.value) is int
                and any(isinstance(target, ast.Name) and target.id == "SCHEMA_VERSION" for target in node.targets)):
            return node.value.value
    return None


def detect_deployment(root):
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return "docker"
    if (root / ".git").exists():
        return "git"
    return "archive"


class Updater:
    def __init__(self, config, *, root, data_dir, version, deployment=None, transport=None, backups=None):
        self.config = config
        self.root = Path(root).resolve()
        self.data_dir = Path(data_dir).resolve()
        self.current = version
        self.backups = backups or Backups(config, data_dir=self.data_dir, version=version)
        self.deployment = deployment or detect_deployment(self.root)
        self._transport = transport
        self._lock = threading.Lock()
        self._check_lock = threading.Lock()
        self._releases = []
        self._checked_at = None
        self._attempted_at = 0.0
        self._error = None
        self._job = {"status": "idle"}
        self._build = None

    # ------------------------------------------------------------------ checks

    @property
    def mirror(self):
        return self.config.get("update_mirror") or ""

    @property
    def custom_mirror(self):
        return self.config.get("update_mirror_custom") or ""

    @property
    def github_token(self):
        return self.config.get("update_github_token") or ""

    def _client(self, timeout=30, token=""):
        headers = {"User-Agent": f"codebuddy2api/{self.current}",
                   "Accept": "application/vnd.github+json"}
        if token:
            # Release listings only: api.github.com is never mirrored, and asset downloads
            # go out without credentials so no mirror ever observes the token.
            headers["Authorization"] = f"Bearer {token}"
        return httpx.Client(transport=self._transport, timeout=timeout, follow_redirects=True,
                            headers=headers)

    def _get(self, url, limit, *, token=""):
        with self._client(token=token) as client, client.stream("GET", url) as response:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > limit:
                    raise UpdateError("响应超过大小限制")
            return bytes(data)

    @staticmethod
    def _release(item):
        if not isinstance(item, dict) or item.get("draft") or item.get("prerelease"):
            return None
        match = TAG.fullmatch(str(item.get("tag_name") or ""))
        if not match:
            return None
        version = match.group(1)
        assets = {asset.get("name"): asset for asset in item.get("assets") or [] if isinstance(asset, dict)}
        archive = assets.get(f"codebuddy2api-{version}.tar.gz")
        digest = archive.get("digest") if archive else None
        published = item.get("published_at")
        return {"tag": f"v{version}", "version": version,
                "name": str(item.get("name") or f"v{version}")[:200],
                "notes": str(item.get("body") or "")[:MAX_NOTES],
                "published_at": published if isinstance(published, str) else None,
                # Built from the fixed repository rather than trusting a returned URL.
                "url": f"{REPOSITORY_URL}/releases/tag/v{version}",
                "installable": archive is not None and "SHA256SUMS" in assets,
                "digest": digest if isinstance(digest, str) and digest.startswith("sha256:") else None}

    def check(self, force=False):
        """Refresh the release list when stale; a forced refresh is still rate limited."""
        with self._check_lock:
            now = time.time()
            if force and now - self._attempted_at < REFRESH_COOLDOWN:
                return self.view()
            if not force and self._checked_at and now - self._checked_at < CHECK_INTERVAL:
                return self.view()
            self._attempted_at = now
            try:
                payload = json.loads(self._get(RELEASES_API, MAX_API_BYTES, token=self.github_token))
                if not isinstance(payload, list):
                    raise ValueError("unexpected releases payload")
                releases = sorted((release for release in map(self._release, payload) if release),
                                  key=lambda release: _version(release["version"]), reverse=True)
                error = None
            except httpx.HTTPStatusError as failure:
                code = failure.response.status_code
                releases, error = None, ("GitHub API 访问频率受限，请稍后重试" if code in (403, 429)
                                         else f"检查更新失败（HTTP {code}）")
            except (httpx.HTTPError, ValueError, UpdateError):
                releases, error = None, "无法连接 GitHub，请检查网络或代理设置（HTTPS_PROXY）"
            build = self.build() or {}
            with self._lock:
                self._error, self._build = error, build
                if releases is not None:
                    self._releases, self._checked_at = releases, now
        return self.view()

    def check_loop(self):
        while True:
            self.check()
            time.sleep(CHECK_INTERVAL)

    def build(self):
        """Where the running code came from, so the WebUI can warn when it may not be the release
        its VERSION names: a git checkout away from that tag, or an image built from main (edge).
        """
        if self.deployment == "git":
            git = shutil.which("git")

            def run(*args):
                try:
                    result = git and subprocess.run([git, "--no-optional-locks", *args], cwd=self.root,
                                                    capture_output=True, text=True, timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    return None
                return result.stdout.strip() if result and result.returncode == 0 else None
            head = run("rev-parse", "HEAD")
            if not head:
                return None
            tag = run("rev-parse", "--verify", "--quiet", f"refs/tags/v{self.current}^{{commit}}")
            dirty = bool(run("status", "--porcelain", "--untracked-files=no"))
            return {"commit": head[:12], "tag_commit": tag[:12] if tag else None, "dirty": dirty,
                    "exact": head == tag and not dirty}
        try:
            info = json.loads((self.root / BUILD_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        # A one-click update replaces the program but not this file, which then describes the image
        # rather than the running release.
        if not isinstance(info, dict) or info.get("version") != self.current:
            return None
        channel, commit = info.get("channel"), info.get("commit")
        return {"channel": channel if channel in ("edge", "stable") else None,
                "commit": commit[:12] if isinstance(commit, str) and commit else None}

    def self_update_block(self):
        """Why this process cannot replace itself, or None when it can."""
        if not os.access(self.root, os.W_OK):
            return "程序目录不可写，无法自动更新"
        if self.config.get("server") is None or not self.config.get("launch"):
            return "当前进程未通过 serve 启动，无法自动重启"
        return None

    def view(self):
        block = self.self_update_block()
        if self._build is None:
            self._build = self.build() or {}
        with self._lock:
            latest = self._releases[0] if self._releases else None
            return {"current": self.current, "repository": REPOSITORY_URL, "deployment": self.deployment,
                    "self_update": {"available": block is None, "reason": block},
                    "checked_at": self._checked_at, "error": self._error,
                    "latest": latest["version"] if latest else None,
                    "update_available": bool(latest and _version(latest["version"]) > _version(self.current)),
                    "releases": [dict(release) for release in self._releases], "job": dict(self._job),
                    "mirror": self.mirror, "mirrors": list(MIRRORS), "mirror_custom": self.custom_mirror,
                    "github_token": bool(self.github_token),
                    "mirror_locked": self.config.get("settings_sources", {}).get("update_mirror")
                    in ("cli", "environment", "dotenv"),
                    "build": self._build or None}

    # ----------------------------------------------------------------- install

    def install(self, tag):
        if (block := self.self_update_block()) is not None:
            raise UpdateError(block)
        with self._lock:
            if self._job["status"] in ("running", "restarting"):
                raise UpdateError("已有更新任务正在进行")
            release = next((item for item in self._releases if item["tag"] == tag), None)
            if release is None:
                raise UpdateError("未找到该版本，请先检查更新")
            if release["version"] == self.current:
                raise UpdateError("已经是该版本")
            if not release["installable"]:
                raise UpdateError("该版本缺少发布包或校验文件")
            self._job = {"status": "running", "step": "download", "target": release["version"],
                         "message": None, "backup": None, "received": 0, "total": None,
                         "started_at": time.time()}
        threading.Thread(target=self._run, args=(release,), daemon=True, name="self-update").start()
        return self.view()

    def _step(self, step, **values):
        with self._lock:
            self._job.update(step=step, **values)

    def _audit(self):
        audit = self.config.get("audit_store")
        return audit if audit is not None and hasattr(audit, "event") else None

    def _run(self, release):
        work = self.root / WORK_DIR
        keep_work = False
        try:
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir()
            name = f"codebuddy2api-{release['version']}.tar.gz"
            mirror = self.mirror
            if mirror and not release["digest"]:
                raise UpdateError("通过镜像下载需要 GitHub 提供的文件摘要；该版本没有摘要，请切换为直连")
            archive = work / name
            digest = self._fetch_release(release, name, archive)
            self._step("verify")
            self._step("extract")
            staged = self._extract(archive, work / "staged", release["version"])
            names = sorted(entry.name for entry in staged.iterdir())
            self._guard_data_dir(names)
            if _version(release["version"]) < _version(self.current):
                self._guard_schema(staged)
            local = self._local_paths(names)
            self._step("backup")
            backup = self.backups.create("update", release["version"])
            self._step("dependencies", backup=backup)
            self._install_dependencies(staged)
            self._step("replace")
            keep_work = True
            self._swap(staged, work / "previous", names)
            self._carry_over(work / "previous", local)
            keep_work = False
            shutil.rmtree(work, ignore_errors=True)
            if audit := self._audit():
                audit.event("admin", "update.installed",
                            {"from": self.current, "to": release["version"], "file": backup})
            self._step("restart", status="restarting")
            request_restart(self.config)
        except Exception as error:
            message = str(error) if isinstance(error, (UpdateError, BackupError)) else f"更新失败：{type(error).__name__}"
            # Clean up before reporting the failure, so nothing sees a failed job with work left behind.
            if not keep_work:
                shutil.rmtree(work, ignore_errors=True)
            with self._lock:
                step = self._job.get("step")
                self._job.update(status="failed", message=message)
            if audit := self._audit():
                audit.event("admin", "update.failed", {"to": release["version"], "code": step})

    def _download(self, url, target):
        digest = hashlib.sha256()
        size = 0
        with self._client() as client, client.stream("GET", url) as response, open(target, "xb") as handle:
            response.raise_for_status()
            # Progress for the WebUI; a mirror may omit or misreport the length, so it is advisory.
            self._step("download", received=0, total=int(response.headers.get("content-length") or 0) or None)
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_ARCHIVE_BYTES:
                    raise UpdateError("发布包超过大小限制")
                digest.update(chunk)
                handle.write(chunk)
                self._step("download", received=size)
        return digest.hexdigest()

    def _fetch_release(self, release, name, archive):
        """Download from the chosen source first, then fall back through the rest.

        Every candidate must reproduce both checksums; a source serving other bytes is
        skipped, so a poisoned mirror cannot block the update while direct still works.
        """
        mirror = self.mirror
        # Mirrored bytes are only trusted against the API digest; without one, direct is the only source.
        if release["digest"]:
            rest = [item for item in MIRRORS if item != mirror]
            candidates = [mirror, "", *rest] if mirror else ["", *rest]
        else:
            candidates = [""]
        failure, verification_failed = None, False
        for candidate in candidates:
            base = f"{candidate}{REPOSITORY_URL}/releases/download/{release['tag']}"
            try:
                sums = self._get(f"{base}/SHA256SUMS", 64 * 1024).decode("utf-8", "replace")
                archive.unlink(missing_ok=True)
                digest = self._download(f"{base}/{name}", archive)
                self._step("verify")
            except (httpx.HTTPError, UpdateError) as error:
                failure = error
                continue
            expected = {parts[1].lstrip("*"): parts[0].lower()
                        for parts in (line.split() for line in sums.splitlines()) if len(parts) == 2}
            if expected.get(name) == digest and release["digest"] in (None, f"sha256:{digest}"):
                return digest
            verification_failed = True
        reason = "发布包校验失败" if verification_failed else failure
        raise UpdateError(f"下载发布包失败，已尝试全部下载源（{reason}）；请测速更换镜像或检查网络"
                          if mirror else
                          f"下载发布包失败（{reason}）；请检查网络或代理设置（HTTPS_PROXY）")

    @staticmethod
    def _extract(archive, destination, version):
        prefix = f"codebuddy2api-{version}"
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            if len(members) > MAX_MEMBERS or sum(member.size for member in members) > MAX_UNPACKED_BYTES:
                raise UpdateError("发布包内容超过限制")
            for member in members:
                parts = PurePosixPath(member.name).parts
                if parts[:1] != (prefix,) or ".." in parts:
                    raise UpdateError("发布包结构异常")
            try:
                # The data filter rejects absolute paths, parent escapes, outside links and devices.
                tar.extractall(destination, filter="data")
            except tarfile.FilterError:
                raise UpdateError("发布包包含不安全的路径") from None
        staged = destination / prefix
        try:
            found = (staged / "VERSION").read_text(encoding="utf-8").strip()
        except OSError:
            found = None
        if (found != version or not (staged / "converter.py").is_file()
                or not (staged / "web" / "dist" / "index.html").is_file()):
            raise UpdateError("发布包缺少必要文件或版本不符")
        return staged

    def _guard_schema(self, staged):
        """An older release must still open the databases this version may have migrated."""
        for database, module in SCHEMAS:
            path = self.data_dir / database
            if not path.exists():
                continue
            with closing(sqlite3.connect(path, timeout=5)) as db:
                current = db.execute("PRAGMA user_version").fetchone()[0]
            supported = _declared_schema(staged / module)
            if supported is None or current > supported:
                raise UpdateError(f"目标版本无法读取当前的 {database}（结构版本 {current}），已中止回滚")

    def _local_paths(self, names):
        """Untracked and ignored paths of a git checkout inside the entries being replaced."""
        if self.deployment != "git":
            return []
        git = shutil.which("git")
        try:
            result = git and subprocess.run([git, "ls-files", "--others", "--directory", "-z"],
                                            cwd=self.root, capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if not result or result.returncode:
            raise UpdateError("无法读取 Git 工作区中的未跟踪文件，已中止更新")
        paths = []
        for raw in result.stdout.split(b"\0"):
            parts = PurePosixPath(os.fsdecode(raw).rstrip("/")).parts
            if len(parts) > 1 and parts[0] in names and "__pycache__" not in parts:
                paths.append(PurePosixPath(*parts))
        return paths

    def _carry_over(self, previous, paths):
        """Move untracked local files out of the replaced entries into the new tree."""
        try:
            for path in paths:
                source, target = previous / path, self.root / path
                if os.path.lexists(target) or not os.path.lexists(source):
                    continue   # The release ships this path now and its copy wins.
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, target)
        except OSError:
            raise UpdateError(f"程序文件已替换，但未能迁回未跟踪的文件；请从 {WORK_DIR}/previous 移回后重启") from None

    def _guard_data_dir(self, names):
        for name in names:
            entry = (self.root / name).resolve()
            if self.data_dir == entry or entry in self.data_dir.parents:
                raise UpdateError("数据目录位于将被替换的程序目录中，无法自动更新")

    def _install_dependencies(self, staged):
        incoming, current = staged / "requirements.txt", self.root / "requirements.txt"
        if not incoming.exists() or (current.exists() and current.read_bytes() == incoming.read_bytes()):
            return
        uv = shutil.which("uv")
        command = ([uv, "pip", "install", "--python", sys.executable] if uv
                   else [sys.executable, "-m", "pip", "install"])
        command += ["--require-hashes", "--only-binary", ":all:", "-r", str(incoming)]
        try:
            result = subprocess.run(command, cwd=self.root, capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired):
            raise UpdateError("依赖安装失败：无法运行安装程序或已超时") from None
        if result.returncode:
            tail = (result.stderr or result.stdout).strip().splitlines()[-1:] or [""]
            raise UpdateError(f"依赖安装失败：{tail[0][:300]}")

    def _swap(self, staged, previous, names):
        """Move each release entry aside and the staged one in; undo every step on failure."""
        previous.mkdir()
        done = []
        try:
            for name in names:
                current = self.root / name
                existed = os.path.lexists(current)
                if existed:
                    self._move_aside(current, previous / name)
                done.append((name, existed))
                os.replace(staged / name, current)
        except OSError as error:
            try:
                for name, existed in reversed(done):
                    if not os.path.lexists(staged / name):    # The new entry made it into place.
                        os.replace(self.root / name, staged / name)
                    if existed:
                        os.replace(previous / name, self.root / name)
            except OSError:
                raise UpdateError("替换程序文件失败且未能完全回滚，请使用备份手动恢复") from error
            raise UpdateError("替换程序文件失败，已回滚到当前版本") from error

    @staticmethod
    def _move_aside(current, saved):
        try:
            os.replace(current, saved)
        except OSError as error:
            # Container image layers (overlayfs) cannot rename a directory they own; copy it out.
            if error.errno != errno.EXDEV or current.is_symlink() or not current.is_dir():
                raise
            shutil.copytree(current, saved, symlinks=True)
            try:
                shutil.rmtree(current)
            except OSError:
                shutil.rmtree(current, ignore_errors=True)
                os.replace(saved, current)   # The copy sits in the writable layer, so it renames back.
                raise

    # ----------------------------------------------------------------- mirrors

    def test_mirrors(self, only=None):
        """Time one source, or every known one, on the newest release archive; the bytes are discarded."""
        if not self._releases:
            self.check()
        with self._lock:
            release = next((item for item in self._releases if item["installable"]), None)
        if release is None:
            raise UpdateError("没有可用于测速的发布包，请先检查更新")
        url = f"{REPOSITORY_URL}/releases/download/{release['tag']}/codebuddy2api-{release['version']}.tar.gz"
        # Direct, the presets, the remembered custom slot and the source in use, each once.
        candidates = [only] if only is not None else list(dict.fromkeys(
            ["", *MIRRORS, *(value for value in (self.custom_mirror, self.mirror) if value)]))
        with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
            results = list(pool.map(lambda mirror: self._probe(mirror, url), candidates))
        return sorted(results, key=lambda item: (not item["ok"], -(item["speed"] or 0)))

    def _probe(self, mirror, url):
        result = {"mirror": mirror, "ok": False, "latency_ms": None, "speed": None, "error": None}
        started = first = time.monotonic()
        received, head = 0, b""
        try:
            with self._client(httpx.Timeout(PROBE_SECONDS, connect=5)) as client, \
                    client.stream("GET", mirror + url) as response:
                if response.status_code != 200:
                    result["error"] = f"HTTP {response.status_code}"
                    return result
                for chunk in response.iter_bytes():
                    if result["latency_ms"] is None:
                        # Throughput excludes connection setup; time-to-first-byte is reported separately.
                        first = time.monotonic()
                        result["latency_ms"] = round((first - started) * 1000)
                    head += chunk[:max(0, 2 - len(head))]
                    received += len(chunk)
                    if received >= PROBE_BYTES or time.monotonic() - started > PROBE_SECONDS:
                        break
        except httpx.TimeoutException:
            result["error"] = "超时"
            return result
        except httpx.HTTPError:
            result["error"] = "无法连接"
            return result
        if head != b"\x1f\x8b":
            # Some mirrors answer 200 with an HTML page instead of the file.
            result["error"] = "返回的不是发布包"
            return result
        result.update(ok=True, speed=round(received / max(time.monotonic() - first, 0.001) / 1024, 1))
        return result
