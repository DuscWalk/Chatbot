import base64
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image

from plugins.astrbot_plugin_rolebot.diagnostics import DebugTraceLogger
from plugins.astrbot_plugin_rolebot.policy import GroupPolicy, duration, intent, voice_requested
from plugins.astrbot_plugin_rolebot.search import SearchEvidence, SearchService, VisionWebSearch
from plugins.astrbot_plugin_rolebot.stickers import StickerLibrary
from plugins.astrbot_plugin_rolebot.vision.image_preprocessor import ImagePreprocessor
from plugins.astrbot_plugin_rolebot.vision.vision_cache import VisionCache
from plugins.astrbot_plugin_rolebot.vision.vision_client import VisualAnalyzer
from plugins.astrbot_plugin_rolebot.vision.vision_pipeline import VisionPipeline
from plugins.astrbot_plugin_rolebot.vision.vision_types import (
    ConfidenceBand,
    ImageDecision,
    ImageFallbackEvidence,
    SearchSource,
    VisionSynthesis,
)


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

    def test_reply_limits_and_random_cooldown_are_per_group(self):
        p = GroupPolicy()
        self.assertTrue(p.can_reply("g", 100))
        self.assertTrue(p.can_random_reply("g", 100))
        for stamp in range(100, 116, 3):
            self.assertTrue(p.can_reply("g", stamp))
            p.record_reply("g", stamp)
            self.assertFalse(p.can_reply("g", stamp + 1))
        self.assertFalse(p.can_reply("g", 130))
        self.assertTrue(p.can_reply("other", 130))
        self.assertTrue(p.can_reply("g", 160))
        self.assertFalse(p.can_random_reply("g", 234))
        self.assertTrue(p.can_random_reply("g", 235))
        self.assertTrue(p.can_random_reply("other", 130))
        p.prune(4000)
        self.assertFalse(p.activity or p.replies)

    def test_busy_group_uses_delivered_messages_and_expires_per_group(self):
        policy = GroupPolicy()
        for stamp in range(100, 106):
            policy.record_reply("napcat:g", stamp)
        self.assertFalse(policy.compact_reply("napcat:g", 106))
        for stamp in range(100, 106):
            policy.record_delivery("napcat:g", stamp)
        self.assertTrue(policy.compact_reply("napcat:g", 106))
        self.assertFalse(policy.compact_reply("napcat:other", 106))
        self.assertFalse(policy.compact_reply("other:g", 106))
        self.assertFalse(policy.compact_reply("napcat:g", 160))
        self.assertTrue(policy.compact_reply("napcat:g", 160, seconds=120))
        self.assertFalse(policy.compact_reply("napcat:g", 106, threshold=7))
        policy.prune(1000)
        self.assertFalse(policy.deliveries)

    def test_repeat_window_and_cross_content_cooldown(self):
        p = GroupPolicy()

        def repeat(text, user, stamp):
            return p.repeat("g", user, text, stamp, window=45, group_cooldown=60)

        self.assertFalse(repeat("x", "a", 100))
        self.assertFalse(repeat("x", "b", 146))
        self.assertTrue(repeat("x", "a", 147))
        self.assertFalse(repeat("y", "a", 150))
        self.assertFalse(repeat("y", "b", 151))
        self.assertFalse(repeat("y", "a", 207))
        self.assertTrue(repeat("y", "b", 208))
        self.assertFalse(repeat("x", "a", 300))
        self.assertFalse(repeat("x", "b", 301))
        self.assertFalse(repeat("x", "a", 747))
        self.assertTrue(repeat("x", "b", 748))

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


class GeneralVisionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = VisionCache(Path(self.tmp.name) / "vision.sqlite3", ttl_seconds=86400)
        await self.cache.init()
        self.pre = ImagePreprocessor(
            timeout_seconds=1,
            max_download_bytes=100000,
            max_image_pixels=10000,
            allow_animation=True,
        )
        buf = io.BytesIO()
        Image.new("RGB", (32, 32), "red").save(buf, format="PNG")
        self.url = "base64://" + base64.b64encode(buf.getvalue()).decode()
        self.image = await self.pre.fetch(self.url)

    def pipeline(self, synthesis, sources=()):
        analyzer = SimpleNamespace(
            synthesize=AsyncMock(return_value=synthesis),
            reevaluate=AsyncMock(return_value=synthesis),
        )
        web = SimpleNamespace(search=AsyncMock(return_value=sources))
        return VisionPipeline(
            preprocessor=self.pre,
            analyzer=analyzer,
            lens_client=None,
            web_client=web,
            cache=self.cache,
            total_timeout_seconds=5,
            multi_timeout_seconds=5,
            lens_timeout_seconds=1,
            model_timeout_seconds=2,
            lens_concurrency=1,
            max_images=4,
            exact_fallback_enabled=False,
            web_fallback_enabled=True,
            max_exact_fallbacks=0,
            max_web_fallbacks=2,
            model_name="synthetic",
            prompt_version="test",
            lens_parser_version="test",
            schema_version="v1",
        )

    async def test_schema_wrapped_values_are_recovered_but_a_schema_is_not_an_observation(self):
        wrapped = {
            "type": "object",
            "properties": {
                "images": [
                    {
                        "image_number": 1,
                        "confidence": "no_identity",
                        "question_answer": "两只猫。",
                    }
                ],
                "combined_answer": "",
            },
        }
        parsed = VisualAnalyzer._parse_synthesis(wrapped, image_numbers=(1,))
        self.assertIn("两只猫", parsed.to_context_text())
        schema = VisualAnalyzer._synthesis_schema()
        self.assertEqual(
            VisualAnalyzer._parse_synthesis(schema, image_numbers=(1,)).images[0].confidence,
            ConfidenceBand.UNAVAILABLE,
        )

    async def test_question_details_survive_handoff_and_cache_without_an_identity(self):
        answer = "Notebook: 3 × 12.50\nCoffee: 2 × 18.00\nTotal after discount: 68.50"
        decision = ImageDecision(
            1,
            ConfidenceBand.NO_IDENTITY,
            scene_description="A receipt",
            question_answer=answer,
        )
        pipe = self.pipeline(VisionSynthesis((decision,)))
        first = await pipe.describe([self.url], user_question="总价是多少", chat_context="")
        cached = await pipe.describe([self.url], user_question="总价是多少", chat_context="")
        self.assertIn(answer, first.context_text)
        self.assertEqual(first.context_text, cached.context_text)
        pipe.analyzer.synthesize.assert_awaited_once()
        pipe.web_client.search.assert_not_awaited()

    async def test_empty_search_does_not_erase_visual_observations(self):
        previous = VisionSynthesis(
            (
                ImageDecision(
                    1,
                    ConfidenceBand.UNCERTAIN,
                    subject_identity="某个虚构角色",
                    needs_web=True,
                    verification_query="某个角色 独特配饰",
                    question_answer="手中拿着三张卡片。",
                ),
            )
        )
        pipe = self.pipeline(previous)
        result = await pipe.describe([self.url], user_question="拿着什么", chat_context="")
        self.assertIn("三张卡片", result.context_text)
        pipe.web_client.search.assert_awaited_once()
        pipe.analyzer.reevaluate.assert_not_awaited()

    async def test_search_reevaluation_receives_original_images(self):
        previous = VisionSynthesis(
            (
                ImageDecision(
                    1,
                    ConfidenceBand.UNCERTAIN,
                    needs_web=True,
                    verification_query="red object",
                ),
            )
        )
        evidence = (SearchSource("Example", "https://example.com", "example.com"),)
        pipe = self.pipeline(previous, evidence)
        await pipe.describe([self.url], user_question="这是什么", chat_context="")
        images = pipe.analyzer.reevaluate.await_args.kwargs["images"]
        self.assertEqual([(n, im.sha256) for n, im in images], [(1, self.image.sha256)])

        # The concrete analyzer sends pixels to the provider on its second pass too.
        analyzer = object.__new__(VisualAnalyzer)
        analyzer.model_name = "synthetic"
        analyzer.enable_thinking = False
        analyzer._request = AsyncMock(
            return_value=(
                {
                    "images": [
                        {
                            "image_number": 1,
                            "confidence": "no_identity",
                            "question_answer": "def f():\n    return 3",
                        }
                    ]
                },
                "",
            )
        )
        result = await analyzer.reevaluate(
            previous,
            (ImageFallbackEvidence(1, web_sources=evidence),),
            images=images,
            timeout_seconds=2,
        )
        content = analyzer._request.await_args.args[0]["messages"][0]["content"]
        pixels = [part["image_url"]["url"] for part in content if part["type"] == "image_url"]
        self.assertEqual(pixels, [self.image.data_url()])
        self.assertIn("def f():\n    return 3", result.to_context_text())

    async def test_web_summary_is_complete_and_separate_from_page_sources(self):
        summary = "前文。" * 200 + "关键辨别线索。"
        sources = (SearchSource("Example", "https://example.com", "example.com"),)
        service = SimpleNamespace(
            _lookup=AsyncMock(return_value=SearchEvidence(summary, sources, "ok"))
        )
        results = await VisionWebSearch(service).search("what is it")
        self.assertEqual(results[0].result_kind, "search_summary")
        self.assertIn("关键辨别线索", VisualAnalyzer._source_line(results[0]))
        self.assertEqual(results[1:], sources)
        self.assertFalse(results[0].url)
        service._lookup.return_value = SearchEvidence(summary=summary)
        self.assertEqual(await VisionWebSearch(service).search("what is it"), ())
