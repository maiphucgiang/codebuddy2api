#!/usr/bin/env python3
"""Media generation contracts, storage, routing and endpoint behaviour."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Allow direct execution.

import asyncio
import base64
import concurrent.futures
import json
import os
import sqlite3
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch
import zlib

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pathlib import Path as _Path

import converter
from app import media_generation, media_store
from app.admin_api import install_admin
from app.control_store import ControlStore
from app.media_store import MediaStore
from tests import test_region_routing as fixtures


def _png(rgb=(0x7C, 0x66, 0xDC)):
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))
    header = struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes(rgb) * 8 for _ in range(8))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


PNG_BYTES = _png()
MP4_BYTES = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00" * 512
COS = "https://cos.example"
IMAGE_PATHS = ("/v2/images/generations", "/v2/images/edits")


class ContractTests(unittest.TestCase):
    """Pure contract checks: lists, bodies, status mapping and URL validation."""

    def test_static_kinds_and_defaults(self):
        self.assertEqual(media_generation.model_kinds("hunyuan-image-v3.0"), ("image",))
        self.assertIn("edit", media_generation.model_kinds("hunyuan-image-v3.0-art"))
        self.assertIn("image", media_generation.model_kinds("hunyuan-image-v3.0-art"))
        self.assertEqual(media_generation.model_kinds("minimax-video-h3"), ("video",))
        self.assertEqual(media_generation.model_kinds("glm-5.1"), ())
        self.assertEqual(media_generation.DEFAULT_IMAGE_MODEL, "hunyuan-image-v3.0")
        self.assertEqual(media_generation.DEFAULT_IMAGE_EDIT_MODEL, "hunyuan-image-v2.0-general-edit")
        self.assertEqual(media_generation.DEFAULT_VIDEO_MODEL, "minimax-video-h3")
        self.assertEqual(media_generation.MEDIA_PROFILES, frozenset({"cn-cli"}))

    def test_catalog_kinds_exact_tags(self):
        self.assertEqual(media_generation.catalog_kinds({"id": "x", "tags": ["text-to-image"]}), ("image",))
        self.assertEqual(media_generation.catalog_kinds({"id": "x", "tags": ["image-to-image"]}), ("edit",))
        self.assertEqual(media_generation.catalog_kinds({"id": "x", "tags": ["text-to-video"]}), ("video",))
        # image-to-video serves video only, never image kinds despite the name.
        self.assertEqual(media_generation.catalog_kinds({"id": "x", "tags": ["image-to-video"]}), ("video",))
        self.assertEqual(media_generation.catalog_kinds(
            {"id": "x", "tags": ["text-to-image", "image-to-image"]}), ("image", "edit"))
        # Substrings and unknown tags never classify as media.
        for tags in (["image"], ["video"], ["preview-image"], ["image-to-video-preview"], [], None):
            self.assertEqual(media_generation.catalog_kinds({"id": "x", "tags": tags}), (), tags)
        self.assertEqual(media_generation.catalog_kinds({"id": "x"}), ())
        self.assertTrue(media_generation.is_media_model("kling-video-v3"))
        self.assertTrue(media_generation.is_media_model(
            "custom-image", [{"id": "custom-image", "tags": ["text-to-image"]}]))
        self.assertFalse(media_generation.is_media_model(
            "custom-image", [{"id": "custom-image", "tags": ["preview-image"]}]))
        self.assertFalse(media_generation.is_media_model(
            "custom-image", [{"id": "custom-image", "tags": ["beta"]}]))

    def test_base_url_validator(self):
        normalize = media_generation.normalize_media_base_url
        self.assertEqual(normalize(""), "")
        self.assertEqual(normalize("  "), "")
        self.assertEqual(normalize("https://media.example.com/"), "https://media.example.com")
        self.assertEqual(normalize("http://10.0.0.1:8787/base/"), "http://10.0.0.1:8787/base")
        for bad in ("example.com", "ftp://x", "https://user:pw@example.com",
                    "https://example.com?a=1", "https://example.com#f", "//example.com"):
            with self.assertRaises(ValueError, msg=bad):
                normalize(bad)

    def test_image_body_shapes(self):
        # hunyuan family: n is always sent, size defaults when missing.
        body = media_generation.image_request_body("hunyuan-image-v3.0", "a cat", size="1024x1024", n=2)
        self.assertEqual(body, {"model": "hunyuan-image-v3.0", "prompt": "a cat",
                                "size": "1024x1024", "n": 2})
        self.assertEqual(media_generation.image_request_body("hunyuan-image-v3.0-art", "a cat"),
                         {"model": "hunyuan-image-v3.0-art", "prompt": "a cat",
                          "size": "1024x1024", "n": 1})
        # gemini family: the CLI sends no extra fields; n and quality are ignored.
        body = media_generation.image_request_body("gemini-3.0-pro-image", "a cat", n=3, quality="high")
        self.assertEqual(body, {"model": "gemini-3.0-pro-image", "prompt": "a cat",
                                "size": "1024x1024"})
        # Other families: response_format plus n and quality when provided.
        body = media_generation.image_request_body("hy-image-3.5", "a cat", n=2, quality="high")
        self.assertEqual(body, {"model": "hy-image-3.5", "prompt": "a cat", "size": "1024x1024",
                                "response_format": "b64_json", "n": 2, "quality": "high"})
        self.assertEqual(media_generation.image_request_body("hy-image-3.5", "a cat"),
                         {"model": "hy-image-3.5", "prompt": "a cat", "size": "1024x1024",
                          "response_format": "b64_json"})
        body = media_generation.image_request_body(
            "hunyuan-image-v2.0-general-edit", "edit", n=1,
            images=["https://cos.example/a.png", "data:image/png;base64,AAA"],
            input_fidelity="high")
        self.assertEqual(body["image"], ["https://cos.example/a.png", "data:image/png;base64,AAA"])
        self.assertEqual(body["input_fidelity"], "high")
        with self.assertRaises(ValueError):
            media_generation.image_request_body("hunyuan-image-v3.0", "")
        with self.assertRaises(ValueError):
            media_generation.image_request_body("hunyuan-image-v3.0", "a cat", images=[])
        # Multiline prompts pass through unchanged; other C0 controls are rejected.
        body = media_generation.image_request_body("hunyuan-image-v3.0", "line1\nline2\r\n\tindented")
        self.assertEqual(body["prompt"], "line1\nline2\r\n\tindented")
        with self.assertRaises(ValueError):
            media_generation.image_request_body("hunyuan-image-v3.0", "bad\x01prompt")
        with self.assertRaises(ValueError):
            media_generation.image_request_body("hunyuan-image-v3.0", "x" * (media_generation.MAX_PROMPT_CHARS + 1))

    def test_control_character_check(self):
        for allowed in ("plain", "tab\tnewline\ncarriage\r", "中文 ~\x7f"):
            self.assertFalse(media_generation.has_control_chars(allowed), repr(allowed))
        for rejected in ("\x00", "a\x01b", "tail\x1f", "\x08", "\x0b", "\x0c", "\x0e"):
            self.assertTrue(media_generation.has_control_chars(rejected), repr(rejected))

    def test_blank_prompts_are_rejected_but_text_is_kept_verbatim(self):
        for blank in ("   ", "\n\t ", " \r\n"):
            with self.assertRaises(ValueError, msg=repr(blank)):
                media_generation.image_request_body("hunyuan-image-v3.0", blank)
            with self.assertRaises(ValueError, msg=repr(blank)):
                media_generation.video_request_body("minimax-video-h3", prompt=blank)
        padded = "  a cat\nwith a hat \n"
        self.assertEqual(media_generation.image_request_body("hunyuan-image-v3.0", padded)["prompt"], padded)
        self.assertEqual(media_generation.video_request_body("minimax-video-h3", prompt=padded)["prompt"],
                         padded)

    def test_image_references_have_their_own_bound(self):
        uri = "data:image/png;base64," + base64.b64encode(b"\x00" * 30000).decode("ascii")
        self.assertGreater(len(uri), media_generation.MAX_PROMPT_CHARS)
        body = media_generation.image_request_body(
            "hunyuan-image-v2.0-general-edit", "edit", images=[uri])
        self.assertEqual(body["image"], [uri])
        self.assertEqual(
            media_generation.video_request_body("minimax-video-h3", image_url=uri)["image_url"], uri)
        with patch.object(media_generation, "MAX_IMAGE_REF_CHARS", len(uri) - 1):
            with self.assertRaises(ValueError):
                media_generation.image_request_body("hunyuan-image-v2.0-general-edit", "edit", images=[uri])
            with self.assertRaises(ValueError):
                media_generation.video_request_body("minimax-video-h3", image_url=uri)
        with self.assertRaises(ValueError):  # Prompts keep the smaller bound.
            media_generation.image_request_body("hunyuan-image-v3.0", "x" * len(uri))

    def test_video_body_shapes(self):
        body = media_generation.video_request_body(
            "minimax-video-h3", prompt="dance", seconds=4, resolution="2K",
            aspect_ratio="16:9", enable_audio=True, image_url="https://cos.example/a.png")
        self.assertEqual(body["watermark"], True)
        self.assertEqual(body["seconds"], 4)
        self.assertEqual(body["extra_parameters"], {"resolution": "2K", "aspect_ratio": "16:9",
                                                    "enable_audio": True})
        self.assertEqual(body["image_url"], "https://cos.example/a.png")
        minimal = media_generation.video_request_body("minimax-video-h3", image_url="https://x/y.png")
        self.assertNotIn("prompt", minimal)
        self.assertNotIn("extra_parameters", minimal)
        for bad in ({"seconds": 0}, {"seconds": 1.5}, {"resolution": "1080P"}):
            with self.assertRaises(ValueError, msg=bad):
                media_generation.video_request_body(
                    "minimax-video-h3", prompt="x",
                    seconds=bad.get("seconds"), resolution=bad.get("resolution"))

    def test_status_mapping_and_parsers(self):
        self.assertEqual(media_generation.map_video_status("processing"), "in_progress")
        self.assertEqual(media_generation.map_video_status("COMPLETED"), "completed")
        self.assertEqual(media_generation.map_video_status("failed"), "failed")
        self.assertEqual(media_generation.map_video_status("weird-state"), "in_progress")
        self.assertEqual(media_generation.map_video_status(None), "in_progress")
        items, credit = media_generation.parse_image_reply(
            {"data": {"data": [{"url": "https://a/1.png", "junk": 1}], "usage": {"credit": 0.5}}})
        self.assertEqual(items, [{"url": "https://a/1.png"}])
        self.assertEqual(credit, 0.5)
        items, credit = media_generation.parse_image_reply(
            {"data": [{"b64_json": "AAA", "revised_prompt": "cat"}]})
        self.assertEqual(items, [{"b64_json": "AAA", "revised_prompt": "cat"}])
        self.assertIsNone(credit)
        with self.assertRaises(ValueError):
            media_generation.parse_image_reply({"data": {"data": []}})
        self.assertEqual(media_generation.parse_video_submit({"data": {"id": " t1 "}}), "t1")
        with self.assertRaises(ValueError):
            media_generation.parse_video_submit({"data": {}})
        status, urls, credit, message = media_generation.parse_video_task(
            {"data": {"status": "failed", "error": "blocked", "usage": {"credit": 2}}})
        self.assertEqual((status, urls, credit, message), ("failed", [], 2.0, "blocked"))
        # An error object is unwrapped to its message field.
        _, _, _, message = media_generation.parse_video_task(
            {"data": {"status": "failed", "error": {"code": 1, "message": "content blocked"}}})
        self.assertEqual(message, "content blocked")
        # The envelope msg is transport-level "ok" and never becomes the task failure.
        _, _, _, message = media_generation.parse_video_task(
            {"code": 0, "msg": "ok", "data": {"status": "processing"}})
        self.assertEqual(message, "")
        status, urls, credit, message = media_generation.parse_video_task(
            {"data": {"status": "completed", "data": [{"url": "https://a/v.mp4"}]}})
        self.assertEqual((status, urls), ("completed", ["https://a/v.mp4"]))
        self.assertIsNone(credit)

    def test_envelope_errors(self):
        envelope = {"code": 10001, "msg": "余额不足"}
        for parse in (media_generation.parse_image_reply, media_generation.parse_video_submit,
                      media_generation.parse_video_task):
            with self.assertRaises(media_generation.MediaUpstreamError) as caught:
                parse(envelope)
            self.assertEqual(caught.exception.upstream_code, 10001)
            self.assertEqual(caught.exception.message, "余额不足")
        # code 0 or absent stays a normal reply.
        self.assertEqual(media_generation.parse_video_submit({"code": 0, "msg": "ok", "id": "t1"}), "t1")


class StoreTests(unittest.TestCase):
    """MediaStore sniffing, eviction, TTL and pagination."""

    def setUp(self):
        self.root = _Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = MediaStore(self.root)
        self.addCleanup(self.store.close)

    def save(self, kind, data, **kwargs):
        kwargs.setdefault("ttl_hours", 72)
        kwargs.setdefault("max_total_bytes", 1 << 30)
        return self.store.save_asset(kind, data, **kwargs)

    def test_sniff_whitelist(self):
        for kind, data, ext in (("image", PNG_BYTES, "png"),
                                ("image", b"\xff\xd8\xffrest", "jpg"),
                                ("image", b"RIFF----WEBPdata", "webp"),
                                ("image", b"GIF89a....", "gif"),
                                ("video", MP4_BYTES, "mp4"),
                                ("video", b"\x1a\x45\xdf\xa3video", "webm")):
            meta = self.save(kind, data)
            self.assertTrue(meta["id"].startswith("img_" if kind == "image" else "vid_"))
            self.assertEqual(len(meta["id"]), 4 + 32)
            self.assertEqual(meta["kind"], kind)
            self.assertTrue(self.store.asset_file(meta).name.endswith(ext), ext)
        for rejected in (b"<html>x</html>", b"", b"PNG-no-magic", MP4_BYTES[:2]):
            with self.assertRaises(ValueError):
                self.save("image", rejected)
        # A video payload must not land as an image asset even with a valid header.
        with self.assertRaises(ValueError):
            self.save("image", MP4_BYTES)
        with self.assertRaises(ValueError):
            self.save("video", PNG_BYTES)

    def test_headers_and_etag(self):
        meta = self.save("image", PNG_BYTES)
        headers = __import__("app.media_store", fromlist=["security_headers"]).security_headers(meta)
        self.assertEqual(headers["Content-Security-Policy"], "sandbox; default-src 'none'")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("immutable", headers["Cache-Control"])
        self.assertEqual(headers["ETag"], f'"{meta["sha256"]}"')
        self.assertEqual(meta["sha256"], __import__("hashlib").sha256(PNG_BYTES).hexdigest())

    def test_eviction_keeps_quota(self):
        size = len(PNG_BYTES)
        self.save("image", PNG_BYTES, max_total_bytes=2 * size)
        first = self.store.list_assets(ttl_hours=72)["items"][0]["id"]
        self.save("image", PNG_BYTES, max_total_bytes=2 * size)
        self.save("image", PNG_BYTES, max_total_bytes=2 * size)
        usage = self.store.usage(ttl_hours=72)
        self.assertLessEqual(usage["total_bytes"], 2 * size + size)  # New write fits after eviction.
        self.assertEqual(usage["images"]["count"], 2)
        self.assertIsNone(self.store.get_asset(first))

    def test_ttl_cleanup_removes_assets_and_tasks(self):
        meta = self.save("image", PNG_BYTES)
        self.store.create_task("t1", "acct", "cn-cli", "m")
        self.store.update_task("t1", status="completed", media_id=meta["id"], credit=1)
        # Retention is derived from created + the current TTL: age both rows past 72h.
        self.store._db.execute("UPDATE assets SET created=?", (time.time() - 73 * 3600,))
        self.store._db.execute("UPDATE tasks SET created=?", (time.time() - 73 * 3600,))
        self.store._db.commit()
        removed = self.store.cleanup(ttl_hours=72, max_total_bytes=1 << 30)
        self.assertEqual(removed["expired_or_evicted"], 1)
        self.assertIsNone(self.store.get_asset(meta["id"]))
        self.assertIsNone(self.store.get_task("t1"))

    def test_expired_assets_leave_listing_and_usage_immediately(self):
        meta = self.save("image", PNG_BYTES)
        self.store._db.execute("UPDATE assets SET created=?", (time.time() - 2 * 3600,))
        self.store._db.commit()
        self.assertEqual(len(self.store.list_assets(ttl_hours=72)["items"]), 1)
        self.assertEqual(self.store.usage(ttl_hours=72)["images"]["count"], 1)
        # Shrinking the TTL hides the row before any sweep, while the file stays for it.
        self.assertEqual(self.store.list_assets(ttl_hours=1)["items"], [])
        self.assertEqual(self.store.list_assets(kind="image", ttl_hours=1)["items"], [])
        usage = self.store.usage(ttl_hours=1)
        self.assertEqual(usage["total_bytes"], 0)
        self.assertEqual(usage["images"], {"count": 0, "bytes": 0})
        self.assertIsNotNone(self.store.get_asset(meta["id"]))

    def test_sweep_incoming_removes_only_stale_files(self):
        incoming = self.store.directory / media_store.INCOMING_DIR
        stale, fresh = incoming / "stale.png", incoming / "fresh.png"
        for file in (stale, fresh):
            file.write_bytes(PNG_BYTES)
        old = time.time() - 2 * 3600
        os.utime(stale, (old, old))
        self.store.sweep_incoming()
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())
        # Housekeeping and a restart reclaim leftovers too.
        os.utime(fresh, (old, old))
        self.store.cleanup(ttl_hours=72, max_total_bytes=1 << 30)
        self.assertFalse(fresh.exists())
        stale.write_bytes(PNG_BYTES)
        os.utime(stale, (old, old))
        reopened = MediaStore(self.root)
        self.addCleanup(reopened.close)
        self.assertFalse(stale.exists())

    def test_unlink_is_best_effort_and_keeps_shard_directories(self):
        meta = self.save("image", PNG_BYTES)
        shard = self.store.asset_file(meta).parent
        self.assertTrue(self.store.delete_asset(meta["id"]))
        self.assertTrue(shard.is_dir())  # Empty shards stay: removing them races with new saves.
        broken, healthy = Mock(), Mock()
        broken.unlink.side_effect = OSError("read-only filesystem")
        MediaStore._unlink([broken, healthy])
        healthy.unlink.assert_called_once()

    def test_is_expired_follows_current_ttl(self):
        meta = self.save("image", PNG_BYTES)
        self.assertFalse(media_store.is_expired(meta, 72))
        self.assertTrue(media_store.is_expired({**meta, "created": time.time() - 2 * 3600}, 1))
        self.assertTrue(media_store.is_expired({}, 72))

    def test_reads_not_blocked_while_payload_is_staged(self):
        existing = self.save("image", PNG_BYTES)
        entered, release = threading.Event(), threading.Event()

        class Stalled(MediaStore):
            def _write_pending(self, ext, data, digest):
                entered.set()
                release.wait(5)
                return super()._write_pending(ext, data, digest)

        stalled = Stalled(self.root)
        self.addCleanup(stalled.close)
        worker = threading.Thread(
            target=lambda: stalled.save_asset("image", b"\xff\xd8\xff" + b"j" * 32,
                                              ttl_hours=72, max_total_bytes=1 << 30))
        worker.start()
        self.assertTrue(entered.wait(2))
        started = time.time()  # The staged write holds no lock: reads must stay fast.
        self.assertEqual(stalled.get_asset(existing["id"])["id"], existing["id"])
        self.assertLess(time.time() - started, 1)
        release.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(stalled.usage(ttl_hours=72)["images"]["count"], 2)

    def test_v0_database_drops_expires_and_keeps_rows(self):
        self.store.close()
        db = sqlite3.connect(self.store.path)
        db.executescript("""
            DROP TABLE assets;
            CREATE TABLE assets(
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL,
                sha256 TEXT NOT NULL, model TEXT, created REAL NOT NULL, expires REAL NOT NULL);
        """)
        db.execute("INSERT INTO assets VALUES('img_old','image','image/png',3,'x','m',?,?)",
                   (time.time(), time.time() + 3600))
        db.execute("PRAGMA user_version=0")
        db.commit()
        db.close()
        reopened = MediaStore(self.store.root)
        self.addCleanup(reopened.close)
        columns = {row[1] for row in reopened._db.execute("PRAGMA table_info(assets)").fetchall()}
        self.assertNotIn("expires", columns)
        self.assertEqual(reopened._db.execute("PRAGMA user_version").fetchone()[0],
                         media_store.SCHEMA_VERSION)
        meta = reopened.get_asset("img_old")
        self.assertEqual(meta["size"], 3)
        self.assertEqual(reopened.usage(ttl_hours=72)["images"]["count"], 1)

    def test_incoming_residue_is_not_counted(self):
        before = self.store.usage(ttl_hours=72)
        stray = self.store.directory / media_store.INCOMING_DIR / "leftover.png"
        stray.write_bytes(PNG_BYTES)
        self.assertEqual(self.store.usage(ttl_hours=72), before)
        stray.unlink()

    def test_delete_clears_bound_tasks(self):
        meta = self.save("video", MP4_BYTES)
        self.store.create_task("t1", "acct", "cn-cli", "m")
        self.store.update_task("t1", status="completed", media_id=meta["id"])
        self.assertTrue(self.store.delete_asset(meta["id"]))
        task = self.store.get_task("t1")
        self.assertEqual(task["status"], "completed")
        self.assertIsNone(task["media_id"])
        self.assertFalse(self.store.delete_asset(meta["id"]))

    def test_pagination_cursors(self):
        for _ in range(3):
            self.save("image", PNG_BYTES)
        page = self.store.list_assets(limit=2, ttl_hours=72)
        self.assertEqual(len(page["items"]), 2)
        self.assertTrue(page["has_more"])
        second = self.store.list_assets(kind="image", cursor=page["next_cursor"], limit=2, ttl_hours=72)
        self.assertEqual(len(second["items"]), 1)
        self.assertFalse(second["has_more"])
        ids = [item["id"] for item in page["items"]] + [item["id"] for item in second["items"]]
        self.assertEqual(len(set(ids)), 3)
        with self.assertRaises(ValueError):
            self.store.list_assets(cursor="not-json", ttl_hours=72)
        with self.assertRaises(ValueError):
            self.store.list_tasks(status="bogus")


class MediaRoutingTests(fixtures.RegionRoutingTests):
    """Endpoint behaviour against a mocked media upstream."""

    def setUp(self):
        super().setUp()
        self.allowed_profiles = {"cn-cli"}
        self.media_root = _Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = MediaStore(self.media_root)
        self.addCleanup(self.store.close)
        self.media_requests = []
        self.image_status = 200
        self.image_statuses = []  # Per-request override; pops take priority over image_status.
        self.image_envelope_error = False   # Image replies become business-error envelopes.
        self.image_urls = None              # Explicit COS urls returned per image item.
        self.video_envelope_error = False   # Video task replies become error envelopes.
        self.video_task_no_url = False      # Completed tasks omit the resource url.
        self.cos_handlers = []              # (suffix, status, content?) rules, first match wins.
        self.downloads = 0
        self.video_task_status = "completed"
        self.enterContext(patch.dict(converter.CONFIG, {
            "media_store": self.store, "media_public_base_url": "",
            "media_max_total_mb": 1024, "media_ttl_hours": 72}))
        self.enterContext(patch.object(converter, "_log", side_effect=lambda *a, **k: None))

    def entry_state(self, uid):
        # pool.entries() copies rows; reread to observe cooldown changes.
        return next(entry for entry in self.pool.entries()
                    if entry["cm"].summary()["uid"] == uid)

    def configure_media(self, *, profiles=("cn-cli", "cn-work", "intl-cli", "intl-work"), **kwargs):
        self.configure(profiles=profiles, **kwargs)

    def handle_upstream(self, request):
        path = request.url.path
        if request.url.host == "cos.example":
            self.downloads += 1
            for suffix, status, *payload in self.cos_handlers:
                if path.endswith(suffix):
                    if status == 200:
                        content = payload[0] if payload else (MP4_BYTES if path.endswith(".mp4") else PNG_BYTES)
                        return httpx.Response(200, content=content)
                    return httpx.Response(status, json={"error": {"message": "cos refused"}})
            content = MP4_BYTES if path.endswith(".mp4") else PNG_BYTES
            return httpx.Response(200, content=content)
        if path in IMAGE_PATHS:
            self.media_requests.append(request)
            if self.image_envelope_error:
                return httpx.Response(200, json={"code": 10001, "msg": "route refused"})
            status = self.image_statuses.pop(0) if self.image_statuses else self.image_status
            if status != 200:
                return httpx.Response(status, json={"error": {"message": "synthetic media failure"}})
            urls = self.image_urls or [f"{COS}/image-0.png"]
            return httpx.Response(200, json={"data": {"data": [
                {"url": url, "revised_prompt": "synthetic"} for url in urls],
                "usage": {"credit": None}}})
        if path == "/v2/videos/generations":
            self.media_requests.append(request)
            body = json.loads(request.read())
            if body.get("model") in media_generation.IMAGE_TEXT_MODELS + media_generation.IMAGE_EDIT_MODELS:
                return httpx.Response(400, json={"error": {"message": "route config not found"}})
            return httpx.Response(200, json={"data": {"id": "task-synthetic-1"}})
        if path == "/v2/videos/tasks":
            self.media_requests.append(request)
            if self.video_envelope_error:
                return httpx.Response(200, json={"code": 10005, "msg": "task query refused"})
            if self.video_task_status == "failed":
                return httpx.Response(200, json={"data": {"status": "failed", "error": "content blocked"}})
            if self.video_task_status == "processing":
                return httpx.Response(200, json={"data": {"status": "processing"}})
            rows = [] if self.video_task_no_url else [{"url": f"{COS}/video-1.mp4"}]
            return httpx.Response(200, json={"data": {
                "status": "completed", "data": rows,
                "usage": {"credit": 62.85}}})
        return super().handle_upstream(request)

    # -- images --------------------------------------------------------------------------

    def test_image_generation_returns_b64_without_base_url(self):
        result = self.client.post("/v1/images/generations", json={"prompt": "a cat"})
        self.assertEqual(result.status_code, 200, result.text)
        body = result.json()
        self.assertNotIn("usage", body)  # Omitted when the upstream reports no credit.
        item = body["data"][0]
        self.assertIn("b64_json", item)
        self.assertEqual(item["revised_prompt"], "synthetic")
        self.assertTrue(base64.b64decode(item["b64_json"]).startswith(b"\x89PNG"))
        self.assertEqual(self.store.usage(ttl_hours=72)["images"]["count"], 0)  # No local writes without a base URL.

    def test_image_generation_persists_with_base_url(self):
        self.enterContext(patch.dict(converter.CONFIG, {"media_public_base_url": "https://media.example.com/"}))
        result = self.client.post("/v1/images/generations", json={"prompt": "a cat", "n": 1})
        self.assertEqual(result.status_code, 200, result.text)
        url = result.json()["data"][0]["url"]
        self.assertTrue(url.startswith("https://media.example.com/v1/media/images/img_"), url)
        served = self.client.get(url.removeprefix("https://media.example.com"))
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.headers["content-type"], "image/png")
        self.assertEqual(served.headers["content-security-policy"], "sandbox; default-src 'none'")
        self.assertEqual(served.headers["x-content-type-options"], "nosniff")
        self.assertIn("immutable", served.headers["cache-control"])
        etag = served.headers["etag"]
        again = self.client.get(url.removeprefix("https://media.example.com"),
                                headers={"If-None-Match": etag})
        self.assertEqual(again.status_code, 304)
        # Explicit b64_json skips persistence even when a base URL is configured.
        explicit = self.client.post("/v1/images/generations",
                                    json={"prompt": "a cat", "response_format": "b64_json"})
        self.assertIn("b64_json", explicit.json()["data"][0])
        self.assertEqual(self.store.usage(ttl_hours=72)["images"]["count"], 1)

    def test_image_edit_shapes_upstream_body(self):
        result = self.client.post("/v1/images/edits", json={
            "prompt": "make it blue", "images": [{"image_url": f"{COS}/source.png"}]})
        self.assertEqual(result.status_code, 200, result.text)
        sent = json.loads(self.media_requests[-1].read())
        self.assertEqual(sent["model"], media_generation.DEFAULT_IMAGE_EDIT_MODEL)
        self.assertEqual(sent["image"], [f"{COS}/source.png"])
        self.assertEqual(self.media_requests[-1].url.path, "/v2/images/edits")

    def test_data_uri_references_pass_and_oversized_ones_fail(self):
        uri = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 30000).decode("ascii")
        self.assertGreater(len(uri), media_generation.MAX_PROMPT_CHARS)
        edited = self.client.post("/v1/images/edits", json={
            "prompt": "make it blue", "images": [{"image_url": uri}]})
        self.assertEqual(edited.status_code, 200, edited.text[:200])
        self.assertEqual(json.loads(self.media_requests[-1].read())["image"], [uri])
        video = self.client.post("/v1/videos", json={"image_url": uri})
        self.assertEqual(video.status_code, 200, video.text[:200])
        self.assertEqual(json.loads(self.media_requests[-1].read())["image_url"], uri)
        sent = len(self.media_requests)
        with patch.object(media_generation, "MAX_IMAGE_REF_CHARS", len(uri) - 1):
            for path, payload in (
                    ("/v1/images/edits", {"prompt": "x", "images": [{"image_url": uri}]}),
                    ("/v1/videos", {"image_url": uri})):
                result = self.client.post(path, json=payload)
                self.assertEqual(result.status_code, 400, (path, result.text[:200]))
        self.assertEqual(len(self.media_requests), sent)

    def test_blank_prompts_never_reach_the_upstream(self):
        source = [{"image_url": f"{COS}/source.png"}]
        for blank in ("   ", "\n \t"):
            for path, payload in (
                    ("/v1/images/generations", {"prompt": blank}),
                    ("/v1/images/edits", {"prompt": blank, "images": source}),
                    ("/v1/videos", {"prompt": blank}),
                    ("/v1/videos", {"prompt": blank, "image_url": f"{COS}/source.png"})):
                result = self.client.post(path, json=payload)
                self.assertEqual(result.status_code, 400, (path, blank, result.text[:200]))
        self.assertEqual(self.media_requests, [])
        # Multi-line prompts keep their whitespace verbatim.
        prompt = "  line one\nline two \n"
        result = self.client.post("/v1/images/generations", json={"prompt": prompt})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(json.loads(self.media_requests[-1].read())["prompt"], prompt)

    def test_image_validation_errors(self):
        cases = [
            ({"prompt": "x", "model": "not-a-model"}, "model_not_found"),
            ({"prompt": "x", "model": "minimax-video-h3"}, "model_not_found"),
            ({"prompt": "x", "n": 9}, None),
            ({"prompt": "x", "response_format": "png"}, None),
            ({"prompt": "", "model": "hunyuan-image-v3.0"}, None),
            ({"images": [], "prompt": "x"}, None),
        ]
        for payload, code in cases:
            path = "/v1/images/edits" if "images" in payload else "/v1/images/generations"
            result = self.client.post(path, json=payload)
            self.assertEqual(result.status_code, 400, (payload, result.text))
            if code:
                self.assertEqual(result.json()["error"]["code"], code)
        self.assertEqual(self.media_requests, [])

    def test_media_models_are_rejected_by_chat(self):
        for path, payload in (
                ("/v1/chat/completions", {"model": "hunyuan-image-v3.0", "messages": [{"role": "user", "content": "hi"}]}),
                ("/v1/messages", {"model": "minimax-video-h3", "messages": [{"role": "user", "content": "hi"}]}),
                ("/v1/responses", {"model": "kling-video-v3", "input": "hi"})):
            result = self.client.post(path, json=payload)
            self.assertEqual(result.status_code, 400, (path, result.text))
            self.assertEqual(result.json()["error"]["code"], "media_model_not_chat")

    def test_api_key_guards_media_endpoints(self):
        self.enterContext(patch.dict(converter.CONFIG, {"api_key": "secret"}))
        self.assertEqual(self.client.post("/v1/images/generations", json={"prompt": "x"}).status_code, 401)
        self.assertEqual(self.client.get("/v1/videos/any").status_code, 401)

    # -- videos --------------------------------------------------------------------------

    def test_video_lifecycle(self):
        self.video_task_status = "processing"
        submitted = self.client.post("/v1/videos", json={"prompt": "dance", "seconds": "4",
                                                         "resolution": "768P"})
        self.assertEqual(submitted.status_code, 200, submitted.text)
        task = submitted.json()
        self.assertEqual(task["object"], "video")
        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["model"], media_generation.DEFAULT_VIDEO_MODEL)
        sent = json.loads(self.media_requests[-1].read())
        self.assertEqual(sent["seconds"], 4)
        self.assertEqual(sent["extra_parameters"]["resolution"], "768P")
        self.assertEqual(sent["watermark"], True)

        first = self.client.get(f"/v1/videos/{task['id']}").json()
        self.assertEqual(first["status"], "in_progress")

        self.video_task_status = "completed"
        completed = self.client.get(f"/v1/videos/{task['id']}").json()
        self.assertEqual(completed["status"], "completed")
        self.assertNotIn("video", completed)  # No base URL configured yet.
        self.assertIn("media_public_base_url", completed["message"])
        self.assertEqual(completed["usage"], {"credit": 62.85})
        self.assertEqual(self.store.usage(ttl_hours=72)["videos"]["count"], 1)  # Video downloads always persist.

        # Configuring the base URL later surfaces the link without another upstream query.
        self.enterContext(patch.dict(converter.CONFIG, {"media_public_base_url": "https://media.example.com"}))
        again = self.client.get(f"/v1/videos/{task['id']}").json()
        self.assertTrue(again["video"]["url"].startswith("https://media.example.com/v1/media/videos/vid_"))
        queries = [r for r in self.media_requests if r.url.path == "/v2/videos/tasks"]
        self.assertEqual(len(queries), 2)  # In-progress check and the completed check.

    def test_video_content_redirect_and_missing_base(self):
        result = self.client.post("/v1/videos", json={"prompt": "dance"})
        task_id = result.json()["id"]
        missing = self.client.get(f"/v1/videos/{task_id}/content")
        self.assertEqual(missing.status_code, 409)
        self.assertEqual(missing.json()["error"]["code"], "media_base_url_missing")
        self.enterContext(patch.dict(converter.CONFIG, {"media_public_base_url": "https://media.example.com"}))
        self.client.get(f"/v1/videos/{task_id}")  # Complete and persist the video.
        redirected = self.client.get(f"/v1/videos/{task_id}/content", follow_redirects=False)
        self.assertEqual(redirected.status_code, 302)
        self.assertTrue(redirected.headers["location"].startswith(
            "https://media.example.com/v1/media/videos/vid_"))

    def test_video_failure_is_terminal(self):
        self.video_task_status = "failed"
        task_id = self.client.post("/v1/videos", json={"prompt": "dance"}).json()["id"]
        first = self.client.get(f"/v1/videos/{task_id}").json()
        self.assertEqual(first["status"], "failed")
        self.assertEqual(first["error"]["message"], "content blocked")
        repeat = self.client.get(f"/v1/videos/{task_id}").json()
        self.assertEqual(repeat, first)
        queries = [r for r in self.media_requests if r.url.path == "/v2/videos/tasks"]
        self.assertEqual(len(queries), 1)

    def test_video_submission_uses_bound_account_after_disable(self):
        task_id = self.client.post("/v1/videos", json={"prompt": "dance"}).json()["id"]
        # Cooldown must not block queries on the bound account.
        entry = self.entries["cn-cli"]
        self.pool.cooldown(entry["cm"], reason="synthetic breaker")
        refreshed = self.client.get(f"/v1/videos/{task_id}")
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        self.assertEqual(refreshed.json()["status"], "completed")

    def test_video_task_account_removed_reports_conflict(self):
        task_id = self.client.post("/v1/videos", json={"prompt": "dance"}).json()["id"]
        self.configure_media(profiles=("cn-work", "intl-cli", "intl-work"))
        result = self.client.get(f"/v1/videos/{task_id}")
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()["error"]["code"], "account_removed")

    def test_video_validation(self):
        cases = [
            {"model": "hunyuan-image-v3.0"},
            {"prompt": "x", "seconds": 0},
            {"prompt": "x", "resolution": "1080P"},
            {"seconds": 4},
            {},
        ]
        for payload in cases:
            result = self.client.post("/v1/videos", json=payload)
            self.assertEqual(result.status_code, 400, (payload, result.text))
        self.assertEqual([r for r in self.media_requests if r.url.path == "/v2/videos/generations"], [])

    def test_deleted_video_asset_reports_without_redownload(self):
        task_id = self.client.post("/v1/videos", json={"prompt": "dance"}).json()["id"]
        self.client.get(f"/v1/videos/{task_id}")
        task = self.store.get_task(task_id)
        self.store.delete_asset(task["media_id"])
        result = self.client.get(f"/v1/videos/{task_id}").json()
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("video", result)
        self.assertIn("已删除", result["message"])

    # -- selection and failover ------------------------------------------------------------

    def test_media_account_requires_profile_catalog_and_credit(self):
        # Only intl accounts exist: no whitelisted media profile.
        self.configure_media(profiles=("intl-cli", "intl-work"))
        result = self.client.post("/v1/images/generations", json={"prompt": "x"})
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json()["error"]["code"], "no_media_account")
        # Whitelisted profile but zero balance.
        self.configure_media(balances={"cn-cli": 0})
        result = self.client.post("/v1/images/generations", json={"prompt": "x"})
        self.assertEqual(result.status_code, 503)
        # A synced root table (serves) without the model excludes the account (#1).
        self.configure_media(balances={"cn-cli": 100})
        self.account_catalogs({"cn-cli": [fixtures.model("shared-model")]},
                              serves={"cn-cli": [{"id": "shared-model",
                                                  "tags": ["text-to-image-preview"]}]})
        result = self.client.post("/v1/images/generations", json={"prompt": "x"})
        self.assertEqual(result.status_code, 503)
        # Media models only in serves (never in the chat picker) still select the
        # account; a tagged catalog entry qualifies even outside the static list.
        self.account_catalogs({"cn-cli": [fixtures.model("shared-model")]},
                              serves={"cn-cli": [{"id": "shared-model"},
                                                 {"id": "custom-video-model",
                                                  "supportsToolCall": False,
                                                  "tags": ["text-to-video"]}]})
        self.assertEqual(self.client.post("/v1/videos", json={"prompt": "x", "model": "custom-video-model",
                                                              "seconds": 4}).status_code, 200)
        # An unsynced account (serves is None) falls back to the static list.
        self.account_catalogs({"cn-cli": [fixtures.model("shared-model")]})
        self.assertEqual(self.client.post("/v1/images/generations", json={"prompt": "x"}).status_code, 200)

    def test_media_kinds_and_guard_use_the_serves_index(self):
        # A chat model carrying an "image"-looking non-media tag stays a chat model.
        self.account_catalogs({"cn-cli": [fixtures.model("shared-model")]},
                              serves={"cn-cli": [
                                  {"id": "shared-model", "tags": ["preview-image"]}]})
        result = self.client.post("/v1/chat/completions", json={
            "model": "shared-model", "messages": [{"role": "user", "content": "hi"}]})
        self.assertNotEqual(result.status_code, 400, result.text[:200])
        # Catalog-only models report kinds and sources from the serves index.
        self.account_catalogs({"cn-cli": [fixtures.model("shared-model")]},
                              serves={"cn-cli": [
                                  {"id": "custom-video-model", "tags": ["text-to-video"]}]})
        models = {entry["id"]: entry for entry in converter.admin_media_models()}
        self.assertEqual(models["custom-video-model"]["kinds"], ["video"])
        self.assertEqual(models["custom-video-model"]["sources"], ["catalog"])
        self.assertEqual(models["custom-video-model"]["default"], [])
        self.assertEqual(models["hunyuan-image-v3.0"]["sources"], ["static"])

    def test_upstream_envelope_errors(self):
        # Image business refusals surface as 502 with the upstream code and never replay.
        self.image_envelope_error = True
        result = self.client.post("/v1/images/generations", json={"prompt": "x"})
        self.assertEqual(result.status_code, 502)
        error = result.json()["error"]
        self.assertEqual(error["upstream_code"], 10001)
        self.assertIn("route refused", error["message"])
        self.assertEqual(len(self.media_requests), 1)
        # A video query envelope error leaves the task untouched for the next poll.
        self.image_envelope_error = False
        task_id = self.client.post("/v1/videos", json={"prompt": "dance"}).json()["id"]
        self.video_envelope_error = True
        refused = self.client.get(f"/v1/videos/{task_id}")
        self.assertEqual(refused.status_code, 502)
        self.assertEqual(refused.json()["error"]["upstream_code"], 10005)
        self.assertEqual(self.store.get_task(task_id)["status"], "queued")
        self.video_envelope_error = False
        recovered = self.client.get(f"/v1/videos/{task_id}").json()
        self.assertEqual(recovered["status"], "completed")

    def test_video_whitelist_failure_is_terminal_and_downloads_once(self):
        task_id = self.client.post("/v1/videos", json={"prompt": "dance"}).json()["id"]
        self.cos_handlers = [(".mp4", 200, b"<html>not a video</html>")]
        first = self.client.get(f"/v1/videos/{task_id}").json()
        self.assertEqual(first["status"], "failed")
        self.assertEqual(first["error"]["message"], "资源类型不在允许的白名单内")
        self.assertEqual(self.downloads, 1)
        repeat = self.client.get(f"/v1/videos/{task_id}").json()
        self.assertEqual(repeat, first)
        self.assertEqual(self.downloads, 1)

    def test_video_completed_without_url_is_terminal(self):
        self.video_task_no_url = True
        task_id = self.client.post("/v1/videos", json={"prompt": "dance"}).json()["id"]
        result = self.client.get(f"/v1/videos/{task_id}").json()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["message"], "上游未返回视频地址")

    def test_concurrent_refreshes_download_one_asset(self):
        task_id = self.client.post("/v1/videos", json={"prompt": "dance"}).json()["id"]
        calls = []

        async def slow_download(url, rid):
            calls.append(url)
            await asyncio.sleep(0.2)
            return MP4_BYTES

        with patch.object(converter, "_media_download_bytes", side_effect=slow_download):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(self.client.get, f"/v1/videos/{task_id}")
                           for _ in range(2)]
                results = [future.result() for future in futures]
        self.assertTrue(all(result.status_code == 200 for result in results), results)
        self.assertEqual(len(calls), 1)  # The per-task lock keeps one download.
        self.assertEqual(self.store.usage(ttl_hours=72)["videos"]["count"], 1)

    def test_retryable_download_failure_recovers_on_next_query(self):
        task_id = self.client.post("/v1/videos", json={"prompt": "dance"}).json()["id"]
        self.cos_handlers = [(".mp4", 503)]
        held = self.client.get(f"/v1/videos/{task_id}").json()
        self.assertNotIn(held["status"], ("completed", "failed"))  # Not a terminal state.
        self.assertIn("重试", held["message"])
        self.assertEqual(self.downloads, 1)
        self.cos_handlers = []
        done = self.client.get(f"/v1/videos/{task_id}").json()
        self.assertEqual(done["status"], "completed")
        self.assertIn("message", done)  # No base URL: the hint stays, no link.
        self.assertNotIn("error", done)

    def new_video_task(self):
        """Submit a video; the simulated upstream reuses one task id, so drop the old row first."""
        self.store._db.execute("DELETE FROM tasks")
        self.store._db.commit()
        self.downloads = 0
        self.cos_handlers = []
        return self.client.post("/v1/videos", json={"prompt": "dance"}).json()["id"]

    def test_cos_signature_gone_is_terminal(self):
        for status in (403, 404, 410):
            with self.subTest(status=status):
                task_id = self.new_video_task()
                self.cos_handlers = [(".mp4", status)]
                first = self.client.get(f"/v1/videos/{task_id}").json()
                self.assertEqual(first["status"], "failed")
                self.assertIn("签名可能已过期", first["error"]["message"])
                self.assertEqual(self.downloads, 1)
                repeat = self.client.get(f"/v1/videos/{task_id}").json()
                self.assertEqual(repeat, first)
                self.assertEqual(self.downloads, 1)  # Terminal: no further download.

    def test_other_download_statuses_stay_retryable(self):
        for status in (408, 429, 400, 502):
            with self.subTest(status=status):
                task_id = self.new_video_task()
                self.cos_handlers = [(".mp4", status)]
                held = self.client.get(f"/v1/videos/{task_id}").json()
                self.assertNotIn(held["status"], ("completed", "failed"))
                self.assertIn("重试", held["message"])
                self.assertEqual(self.downloads, 1)
                self.cos_handlers = []
                done = self.client.get(f"/v1/videos/{task_id}").json()
                self.assertEqual(done["status"], "completed")
                self.assertEqual(self.downloads, 2)

    def test_store_write_failures_keep_the_paid_task_retryable(self):
        for error in (OSError("disk full"), sqlite3.OperationalError("database is locked"),
                      RuntimeError("busy")):
            with self.subTest(error=type(error).__name__):
                task_id = self.new_video_task()
                with patch.object(self.store, "save_asset", side_effect=error):
                    held = self.client.get(f"/v1/videos/{task_id}").json()
                self.assertNotIn(held["status"], ("completed", "failed"))
                self.assertIn("重试", held["message"])
                done = self.client.get(f"/v1/videos/{task_id}").json()
                self.assertEqual(done["status"], "completed")

    def test_malformed_download_url_is_terminal(self):
        with self.assertRaises(converter.MediaDownloadError):
            asyncio.run(converter._media_download_bytes("http://[::1/video.mp4", "rid"))

    def test_in_progress_task_has_no_message_without_a_hint(self):
        self.video_task_status = "processing"
        task_id = self.client.post("/v1/videos", json={"prompt": "dance"}).json()["id"]
        body = self.client.get(f"/v1/videos/{task_id}").json()
        self.assertEqual(body["status"], "in_progress")
        self.assertNotIn("message", body)

    def test_task_swept_during_refresh_answers_404(self):
        task_id = self.client.post("/v1/videos", json={"prompt": "dance"}).json()["id"]
        update = self.store.update_task

        def update_then_sweep(*args, **kwargs):
            update(*args, **kwargs)
            self.store._db.execute("DELETE FROM tasks")
            self.store._db.commit()

        with patch.object(self.store, "update_task", side_effect=update_then_sweep):
            result = self.client.get(f"/v1/videos/{task_id}")
        self.assertEqual(result.status_code, 404, result.text)
        self.assertEqual(result.json()["error"]["code"], "task_not_found")

    def test_partial_image_failure_returns_survivors(self):
        self.enterContext(patch.dict(converter.CONFIG, {"media_public_base_url": "https://media.example.com"}))
        self.image_urls = [f"{COS}/a.png", f"{COS}/b.png"]
        self.cos_handlers = [("b.png", 404)]
        result = self.client.post("/v1/images/generations", json={"prompt": "x", "n": 2})
        self.assertEqual(result.status_code, 200, result.text)
        data = result.json()["data"]
        self.assertEqual(len(data), 1)              # The surviving item still serves a link.
        self.assertTrue(data[0]["url"].startswith("https://media.example.com/v1/media/images/"))
        self.assertEqual(self.store.usage(ttl_hours=72)["images"]["count"], 1)

    def test_total_image_failure_leaves_no_orphans(self):
        self.enterContext(patch.dict(converter.CONFIG, {"media_public_base_url": "https://media.example.com"}))
        self.image_urls = [f"{COS}/a.png", f"{COS}/b.png"]
        self.cos_handlers = [("a.png", 404), ("b.png", 500)]
        result = self.client.post("/v1/images/generations", json={"prompt": "x", "n": 2})
        self.assertEqual(result.status_code, 502)
        self.assertIn("全部 2 张", result.json()["error"]["message"])
        self.assertEqual(self.store.usage(ttl_hours=72)["images"]["count"], 0)

    def test_extra_upstream_items_do_not_mask_a_total_failure(self):
        self.image_urls = [f"{COS}/{name}.png" for name in "abcde"]
        self.cos_handlers = [(".png", 404)]
        result = self.client.post("/v1/images/generations", json={"prompt": "x", "n": 4})
        self.assertEqual(result.status_code, 502, result.text[:200])
        self.assertIn("全部 4 张", result.json()["error"]["message"])
        self.assertEqual(self.downloads, 4)  # Only the first MAX_IMAGE_ITEMS are processed.

    def test_unexpected_item_error_keeps_the_survivors(self):
        self.enterContext(patch.dict(converter.CONFIG, {"media_public_base_url": "https://media.example.com"}))
        self.image_urls = [f"{COS}/a.png", f"{COS}/b.png"]
        download = converter._media_download_bytes

        async def flaky(url, rid):
            if url.endswith("b.png"):
                raise httpx.InvalidURL("synthetic")
            return await download(url, rid)

        with patch.object(converter, "_media_download_bytes", side_effect=flaky):
            result = self.client.post("/v1/images/generations", json={"prompt": "x", "n": 2})
        self.assertEqual(result.status_code, 200, result.text)
        data = result.json()["data"]
        self.assertEqual(len(data), 1)
        asset_id = data[0]["url"].rsplit("/", 1)[-1]
        self.assertIsNotNone(self.store.get_asset(asset_id))  # The survivor stays referenced.
        self.assertEqual(self.store.usage(ttl_hours=72)["images"]["count"], 1)

    def test_store_unavailable_refuses_before_upstream(self):
        self.enterContext(patch.dict(converter.CONFIG, {
            "media_public_base_url": "https://media.example.com", "media_store": None}))
        result = self.client.post("/v1/images/generations", json={"prompt": "x"})
        self.assertEqual(result.status_code, 503)
        self.assertEqual(self.media_requests, [])   # Nothing billed upstream.

    def test_expired_asset_reads_404_immediately(self):
        self.enterContext(patch.dict(converter.CONFIG, {"media_public_base_url": "https://media.example.com"}))
        url = self.client.post("/v1/images/generations", json={"prompt": "x"}).json()["data"][0]["url"]
        path = url.removeprefix("https://media.example.com")
        self.assertEqual(self.client.get(path).status_code, 200)
        self.store._db.execute("UPDATE assets SET created=?", (time.time() - 73 * 3600,))
        self.store._db.commit()
        self.assertEqual(self.client.get(path).status_code, 404)  # Not served until the sweep.

    def test_no_replay_on_server_errors(self):
        self.enterContext(patch.dict(converter.CONFIG, {"failover_max": 2}))
        self.image_status = 500
        result = self.client.post("/v1/images/generations", json={"prompt": "x"})
        self.assertEqual(result.status_code, 500)
        self.assertEqual(len(self.media_requests), 1)

    def test_replay_on_explicit_refusals_and_cooldown_isolation(self):
        self.add_account("cn2", "cn-cli")
        self.configure_media(profiles=("cn-cli", "cn2", "cn-work", "intl-cli", "intl-work"))
        self.enterContext(patch.dict(converter.CONFIG, {"failover_max": 2}))
        # A 403 replays but never cools the account down.
        self.image_statuses = [403]
        result = self.client.post("/v1/images/generations", json={"prompt": "x"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(len(self.media_requests), 2)
        for uid in ("cn-cli", "cn2"):
            self.assertLess(self.entry_state(uid)["fail_until"], time.time(), uid)
        # A 401 replays the same way and does cool the first account down.
        self.image_statuses = [401]
        first_uid = self.media_requests[-2].headers["x-user-id"]  # First attempt of the 401 pair.
        result = self.client.post("/v1/images/generations", json={"prompt": "x"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertGreater(self.entry_state(first_uid)["fail_until"], time.time())
        # 401 with no replacement account left surfaces the original status.
        self.image_status = 401
        self.configure_media(profiles=("cn-cli", "cn-work", "intl-cli", "intl-work"))
        result = self.client.post("/v1/images/generations", json={"prompt": "x"})
        self.assertEqual(result.status_code, 401)


    def test_admin_media_view_lists_models_and_usage(self):
        result = self.client.post("/v1/images/generations", json={"prompt": "x"})
        self.assertEqual(result.status_code, 200, result.text)
        models = converter.admin_media_models()
        by_id = {entry["id"]: entry for entry in models}
        self.assertEqual(by_id["hunyuan-image-v3.0"]["kinds"], ["image"])
        self.assertEqual(by_id["hunyuan-image-v3.0"]["default"], ["image"])
        self.assertEqual(by_id["hunyuan-image-v3.0"]["sources"], ["static"])
        self.assertGreaterEqual(by_id["hunyuan-image-v3.0"]["accounts"], 1)
        self.assertEqual(by_id["hunyuan-image-v2.0-general-edit"]["default"], ["edit"])
        self.assertEqual(by_id["minimax-video-h3"]["default"], ["video"])
        self.assertNotIn("glm-5.1", by_id)


class AdminMediaApiTests(unittest.TestCase):
    """/admin/media routes through the installed management API."""

    def setUp(self):
        self.root = _Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = MediaStore(self.root)
        self.addCleanup(self.store.close)
        self.control = ControlStore(self.root / "control.sqlite3")
        self.addCleanup(self.control.close)
        self.config = {"api_key": "synthetic-key", "control_store": self.control,
                       "media_store": self.store, "media_public_base_url": "https://media.example.com",
                       "media_max_total_mb": 1024, "media_ttl_hours": 72,
                       "session_path": self.root / "admin-sessions.json"}
        audit = Mock()
        audit.storage.return_value = {"db_bytes": 0}
        self.config["audit_store"] = audit
        self.video_task = {"task_id": "t1", "account_key": "acct", "profile": "cn-cli",
                           "model": "minimax-video-h3", "created": time.time(),
                           "updated": time.time(), "status": "completed",
                           "media_id": None, "error": None, "credit": 3.5}
        self.gateway = Mock()
        self.gateway._media_ttl_hours.side_effect = lambda: float(self.config["media_ttl_hours"])
        self.gateway._media_max_bytes.side_effect = lambda: self.config["media_max_total_mb"] * 1048576
        self.gateway.admin_media_models.return_value = [{"id": "minimax-video-h3", "kinds": ["video"],
                                                         "sources": ["static"], "default": ["video"],
                                                         "accounts": 2}]
        self.gateway._media_video_refresh = AsyncMock(return_value=self.video_task)
        self.gateway._media_video_response = Mock(
            side_effect=lambda task, meta=None: {"id": task["task_id"], "status": task["status"]})
        self.app = FastAPI()
        install_admin(self.app, self.config, self.gateway)
        self.client = self.enterContext(TestClient(self.app))
        session = self.client.post("/admin/session", json={"api_key": "synthetic-key"},
                                   headers={"Origin": "http://testserver"})
        self.headers = {"Origin": "http://testserver", "X-CSRF-Token": session.json()["csrf_token"]}

    def test_summary_requires_session_and_reports_state(self):
        anonymous = TestClient(self.app)  # The shared client keeps its session cookie.
        self.assertEqual(anonymous.get("/admin/media").status_code, 401)
        anonymous.close()
        result = self.client.get("/admin/media", headers=self.headers)
        self.assertEqual(result.status_code, 200, result.text)
        body = result.json()
        self.assertTrue(body["configured"])
        self.assertEqual(body["base_url"], "https://media.example.com")
        self.assertFalse(body["locked"])
        self.assertEqual(body["limit_bytes"], 1024 * 1048576)
        self.assertEqual(body["tasks"], {"active": 0, "failed": 0})
        self.assertEqual(body["models"][0]["id"], "minimax-video-h3")

    def test_asset_listing_serving_and_delete(self):
        meta = self.store.save_asset("image", PNG_BYTES, model="m", ttl_hours=1, max_total_bytes=1 << 20)
        listed = self.client.get("/admin/media/assets", headers=self.headers).json()
        self.assertEqual(listed["items"][0]["public_url"],
                         f"https://media.example.com/v1/media/images/{meta['id']}")
        served = self.client.get(f"/admin/media/assets/{meta['id']}/content", headers=self.headers)
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.headers["content-security-policy"], "sandbox; default-src 'none'")
        downloaded = self.client.get(f"/admin/media/assets/{meta['id']}/content?download=1",
                                     headers=self.headers)
        self.assertIn("attachment", downloaded.headers["content-disposition"])
        missing = self.client.get("/admin/media/assets/img_doesnotexist/content", headers=self.headers)
        self.assertEqual(missing.status_code, 404)
        deleted = self.client.delete(f"/admin/media/assets/{meta['id']}", headers=self.headers)
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.client.get("/admin/media/assets", headers=self.headers)
                         .json()["items"], [])
        self.assertEqual(
            self.client.get("/admin/media/assets?kind=bogus", headers=self.headers).status_code, 400)

    def test_task_listing_and_refresh(self):
        self.store.create_task("t1", "acct", "cn-cli", "minimax-video-h3")
        listed = self.client.get("/admin/media/tasks", headers=self.headers).json()
        self.assertEqual(listed["items"][0]["task_id"], "t1")
        refreshed = self.client.post("/admin/media/tasks/t1/refresh", headers=self.headers)
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        self.gateway._media_video_refresh.assert_called_once()
        missing = self.client.post("/admin/media/tasks/missing/refresh", headers=self.headers)
        self.gateway._media_video_refresh.side_effect = converter.HTTPException(status_code=404, detail={
            "error": {"message": "视频任务不存在或已过期"}})
        missing = self.client.post("/admin/media/tasks/missing/refresh", headers=self.headers)
        self.assertEqual(missing.status_code, 404)
        self.assertIn("视频任务不存在", missing.json()["error"]["message"])

    def test_expired_assets_disappear_from_admin_views(self):
        meta = self.store.save_asset("image", PNG_BYTES, model="m", ttl_hours=72, max_total_bytes=1 << 20)
        self.store._db.execute("UPDATE assets SET created=?", (time.time() - 2 * 3600,))
        self.store._db.commit()
        path = f"/admin/media/assets/{meta['id']}"

        def views():
            listed = self.client.get("/admin/media/assets", headers=self.headers).json()["items"]
            usage = self.client.get("/admin/media", headers=self.headers).json()["usage"]
            return (len(listed), self.client.get(path, headers=self.headers).status_code,
                    self.client.get(path + "/content", headers=self.headers).status_code,
                    usage["images"]["count"])

        self.assertEqual(views(), (1, 200, 200, 1))
        self.config["media_ttl_hours"] = 1  # Shrinking the TTL applies before any sweep.
        self.assertEqual(views(), (0, 404, 404, 0))

    def test_media_routes_keep_the_generic_error_envelope(self):
        refresh = self.gateway._media_video_refresh
        refresh.side_effect = converter.HTTPException(status_code=409, detail="plain text detail")
        result = self.client.post("/admin/media/tasks/t1/refresh", headers=self.headers)
        self.assertEqual(result.status_code, 409)
        self.assertTrue(result.json()["error"]["message"])
        refresh.side_effect = RuntimeError("secret internal detail")
        result = self.client.post("/admin/media/tasks/t1/refresh", headers=self.headers)
        self.assertEqual(result.status_code, 500)
        self.assertEqual(result.json()["error"]["message"], "管理操作失败，请检查存储状态后重试")
        self.assertNotIn("secret", result.text)

    def test_unavailable_store_reports_service_error(self):
        self.config["media_store"] = None
        for method, path in (("GET", "/admin/media/assets"), ("GET", "/admin/media/tasks"),
                             ("GET", "/admin/media/assets/img_x/content"),
                             ("DELETE", "/admin/media/assets/img_x")):
            result = self.client.request(method, path, headers=self.headers)
            self.assertEqual(result.status_code, 503, path)
            self.assertIn("媒体存储不可用", result.json()["error"]["message"])


if __name__ == "__main__":
    unittest.main()
