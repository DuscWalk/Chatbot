import base64
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from PIL import Image

from plugins.astrbot_plugin_rolebot.diagnostics import DebugTraceLogger
from plugins.astrbot_plugin_rolebot.policy import GroupPolicy, duration, intent, voice_requested
from plugins.astrbot_plugin_rolebot.search import SearchEvidence, SearchService
from plugins.astrbot_plugin_rolebot.stickers import StickerLibrary
from plugins.astrbot_plugin_rolebot.vision.image_preprocessor import ImagePreprocessor
from plugins.astrbot_plugin_rolebot.vision.vision_types import SearchSource


class RolebotPolicyTests(unittest.TestCase):
    def test_daily_chat_does_not_search_or_return_clock(self):
        for text in [
            "你现在心情怎么样",
            "恢复需要多长时间",
            "这段时间有点忙",
            "别联网，陪我聊两句",
        ]:
            self.assertEqual(intent(text), "none", text)
        self.assertEqual(intent("现在几点了"), "time")
        self.assertEqual(intent("明天南京天气怎么样"), "search")
        self.assertEqual(intent("搜一下最新版本"), "search")
        self.assertTrue(voice_requested("发条语音给我听"))
        self.assertFalse(voice_requested("不要发语音"))

    def test_repeat_requires_consecutive_different_people_and_respects_cooldown(self):
        p = GroupPolicy()
        self.assertFalse(p.repeat("g", "a", "x", 1))
        self.assertFalse(p.repeat("g", "a", "x", 2))
        self.assertFalse(p.repeat("g", "b", "", 3))
        self.assertFalse(p.repeat("g", "b", "x", 4))
        self.assertTrue(p.repeat("g", "a", "x", 5))
        self.assertFalse(p.repeat("g", "c", "x", 6))
        self.assertFalse(p.repeat("other", "c", "x", 7))
        self.assertTrue(p.repeat("g", "b", "x", 606))
        self.assertEqual(duration("2h"), 7200)
        with self.assertRaises(ValueError):
            duration("99999999h")

    def test_missing_sources_never_masquerade_as_verified_search(self):
        evidence = SearchEvidence(summary="unverified model output")
        self.assertNotIn("unverified model output", evidence.context())
        self.assertIn("未得到", evidence.context())

    def test_sticker_paths_cannot_escape_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "stickers"
            root.mkdir()
            (Path(tmp) / "outside.png").write_bytes(b"synthetic")
            manifest = root / "manifest.yaml"
            manifest.write_text("items:\n  - id: escape\n    file: ../outside.png\n")
            self.assertEqual(StickerLibrary(root=root, manifest_path=manifest).items(), [])

    def test_metadata_traces_do_not_save_user_text_or_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = DebugTraceLogger(root_dir=Path(tmp), include_content=False)
            trace = log.start_trace({"prompt": "PRIVATE", "scene": "private"})
            trace.event(
                "request",
                {"prompt": "PRIVATE", "url": "https://example.com/?secret=PRIVATE", "ok": True},
            )
            text = "".join(p.read_text() for p in Path(tmp).rglob("*.jsonl"))
            self.assertNotIn("PRIVATE", text)
            self.assertIn('"ok": true', text)


class RolebotMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_animation_is_sampled_and_local_references_work(self):
        frames = [Image.new("RGB", (20, 10), color) for color in ("red", "blue", "green")]
        data = io.BytesIO()
        frames[0].save(
            data, format="GIF", save_all=True, append_images=frames[1:], duration=50, loop=0
        )
        pre = ImagePreprocessor(
            timeout_seconds=1,
            max_download_bytes=1024 * 1024,
            max_image_pixels=100000,
            allow_animation=True,
        )
        encoded = "base64://" + base64.b64encode(data.getvalue()).decode()
        result = await pre.fetch(encoded)
        self.assertTrue(result.animated)
        self.assertEqual(result.width, 60)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.gif"
            path.write_bytes(data.getvalue())
            local = await pre.fetch(str(path))
            self.assertEqual(local.sha256, result.sha256)

    async def test_search_cache_and_cooldown_are_per_session(self):
        search = SearchService(None, {})
        search._lookup = AsyncMock(
            return_value=SearchEvidence(
                "summary", (SearchSource("Title", "https://example.com", "example.com"),), "ok"
            )
        )
        try:
            await search.lookup("query", "a", {})
            await search.lookup("query", "a", {})
            self.assertEqual(search._lookup.await_count, 1)
            await search.lookup("query", "b", {})
            self.assertEqual(search._lookup.await_count, 2)
            denied = await search.lookup("different query", "a", {})
            self.assertEqual(denied.status, "cooldown")
        finally:
            await search.close()
