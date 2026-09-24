"""Meaningful hook tests with real AstrBot events and request objects."""

import asyncio
import copy
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.core_lifecycle import AstrBotCoreLifecycle  # noqa: F401
from astrbot.core.message.message_event_result import ResultContentType
from astrbot.core.pipeline.context import PipelineContext
from astrbot.core.pipeline.respond.stage import RespondStage
from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.provider.entities import ProviderRequest
from lemuen import PRIVATE_CHAT_SETTINGS

from plugins.astrbot_plugin_lemuen.main import LemuenPlugin
from plugins.astrbot_plugin_lemuen.render import (
    CONTEXT_MARKER,
    SPEAKER_PREFIX,
    compile_style,
    retrieval_query,
    session_allowed,
)


class PluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        message = AstrBotMessage()
        message.type = MessageType.GROUP_MESSAGE
        message.sender = MessageMember("alice", "同名同学")
        message.group_id = "test-group"
        message.message = []
        self.event = AstrMessageEvent(
            "我是不是一定要原谅她？",
            message,
            PlatformMetadata(name="aiocqhttp", description="test", id="test"),
            "group",
        )
        self.config = {
            "enabled": True,
            "allowed_sessions": [self.event.unified_msg_origin],
            "persona_id": "蕾缪安",
            "knowledge_base": "蕾缪安",
        }
        self.context = SimpleNamespace(
            get_config=lambda *args: {"provider_settings": {}},
            persona_manager=SimpleNamespace(
                resolve_selected_persona=AsyncMock(return_value=("蕾缪安", None, None, False))
            ),
            kb_manager=SimpleNamespace(
                retrieve=AsyncMock(
                    return_value={
                        "results": [{"content": "# L061 · 蕾缪安\n为自己说错的话向小菲道歉"}]
                    }
                )
            ),
        )
        self.plugin = LemuenPlugin(self.context, self.config)
        self.req = ProviderRequest(
            prompt="test",
            system_prompt="原生人格和其他系统设置",
            conversation=SimpleNamespace(persona_id="蕾缪安"),
        )

    async def test_all_private_pattern_includes_new_friends_but_not_groups(self):
        self.config["allowed_sessions"] = ["napcat:FriendMessage:*"]
        self.event.platform_meta.id = "napcat"
        self.event.session.platform_name = "napcat"
        self.event.session.platform_id = "napcat"
        await self.plugin.on_request(self.event, self.req)
        self.context.kb_manager.retrieve.assert_not_awaited()
        self.event.message_obj.group_id = ""
        self.event.message_obj.type = MessageType.FRIEND_MESSAGE
        self.event.session.message_type = MessageType.FRIEND_MESSAGE
        self.event.session.session_id = "new-friend"
        await self.plugin.on_request(self.event, self.req)
        self.context.kb_manager.retrieve.assert_awaited_once()
        for umo in ["napcat:FriendMessage:new-friend", "napcat:FriendMessage:another:friend"]:
            self.assertTrue(session_allowed(umo, self.config["allowed_sessions"]))
        for umo in [
            "napcat:GroupMessage:123",
            "other:FriendMessage:123",
            "webchat:FriendMessage:123",
        ]:
            self.assertFalse(session_allowed(umo, self.config["allowed_sessions"]))

    async def test_disabled_scope_and_other_persona_do_not_change_requests(self):
        for config in [{"enabled": False}, {"allowed_sessions": []}]:
            saved = self.config.copy()
            self.config.update(config)
            await self.plugin.on_request(self.event, self.req)
            self.config.update(saved)
        self.context.persona_manager.resolve_selected_persona.return_value = (
            "其他人格",
            None,
            None,
            False,
        )
        await self.plugin.on_request(self.event, self.req)
        self.context.kb_manager.retrieve.assert_not_awaited()
        self.assertEqual(self.req.system_prompt, "原生人格和其他系统设置")
        self.assertFalse(self.req.extra_user_content_parts)

    async def test_tool_free_persona_also_disables_platform_added_tools(self):
        self.context.persona_manager.resolve_selected_persona.return_value = (
            "蕾缪安",
            {"tools": []},
            None,
            False,
        )
        self.req.func_tool = SimpleNamespace(tools=["platform-send-tool"])
        await self.plugin.on_request(self.event, self.req)
        self.assertIsNone(self.req.func_tool)

    async def test_native_request_preserved_and_reference_conditional(self):
        await self.plugin.on_request(self.event, self.req)
        self.assertTrue(self.req.system_prompt.startswith("原生人格和其他系统设置"))
        details = self.event.get_extra("lemuen")
        self.assertIn("B05", details["pattern_ids"])
        self.assertIn("L061", details["entry_ids"])
        self.assertEqual(self.req.prompt, "test")
        marker = self.req.extra_user_content_parts[-1].text
        self.assertEqual(json.loads(marker.removeprefix(SPEAKER_PREFIX))["id"], "alice")
        await self.plugin.on_request(self.event, self.req)
        self.assertEqual(self.req.system_prompt.count(CONTEXT_MARKER), 1)
        self.context.kb_manager.retrieve.assert_awaited_once()

    async def test_retrieval_failure_keeps_persona_without_friendship_examples(self):
        for error, status in [(TimeoutError(), "timeout"), (ValueError("private-data"), "error")]:
            req = ProviderRequest(system_prompt="原生人格")
            self.context.kb_manager.retrieve.side_effect = error
            await self.plugin.on_request(self.event, req)
            self.assertTrue(req.system_prompt.startswith("原生人格"))
            self.assertEqual(self.event.get_extra("lemuen")["retrieval"], status)
            self.assertNotIn("B05", self.event.get_extra("lemuen")["pattern_ids"])
            self.assertNotIn("private-data", req.system_prompt)

    async def test_timeout_is_enforced_and_empty_results_are_safe(self):
        async def stalled(**kwargs):
            await asyncio.Event().wait()

        self.config["retrieval_timeout_seconds"] = 1
        self.context.kb_manager.retrieve.side_effect = stalled
        await asyncio.wait_for(self.plugin.on_request(self.event, self.req), timeout=3)
        self.assertEqual(self.event.get_extra("lemuen")["retrieval"], "timeout")
        self.context.kb_manager.retrieve.side_effect = None
        self.context.kb_manager.retrieve.return_value = {}
        await self.plugin.on_request(self.event, ProviderRequest(system_prompt="原生人格"))
        self.assertEqual(self.event.get_extra("lemuen")["retrieval"], "empty")
        self.assertEqual(self.event.get_extra("lemuen")["entry_ids"], [])

    async def test_existing_native_kb_is_reused(self):
        self.req.extra_user_content_parts = [
            {
                "type": "text",
                "text": "[Related Knowledge Base Results]:\n【知识 1】\n来源: 蕾缪安 / 旧友.md\n"
                "内容: # L061 · 蕾缪安\n旧友的分歧\n相关度: 0.2\n",
            }
        ]
        await self.plugin.on_request(self.event, self.req)
        self.context.kb_manager.retrieve.assert_not_awaited()
        self.assertEqual(self.event.get_extra("lemuen")["entry_ids"], ["L061"])
        self.assertNotIn("旧友的分歧", self.req.system_prompt)

    async def test_native_bubbles_preserve_text_code_long_answers_and_commands(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["platform_settings"]["segmented_reply"].update(
            PRIVATE_CHAT_SETTINGS["platform_settings"]["segmented_reply"]
        )
        context = PipelineContext(
            config,
            SimpleNamespace(
                context=SimpleNamespace(get_using_tts_provider_async=AsyncMock(return_value=None))
            ),
            "default",
        )
        decorate, respond = ResultDecorateStage(), RespondStage()
        await decorate.initialize(context)
        await respond.initialize(context)
        respond.interval = [0, 0]
        code = "说明。\n\n```python\nx = 1\n\nprint(x)\n```\n\n结束。"
        long_text = "长内容。" * 160 + "\n\n结尾。"
        cases = [
            ("晚安，博士。", True, ["晚安，博士。"]),
            ("回来了？\n\n去了哪里？树影好看吗？", True, ["回来了？", "去了哪里？树影好看吗？"]),
            ("一。\n\n二。\n\n三。\n\n四。", True, ["一。", "二。", "三。", "四。"]),
            (code, True, [code]),
            (long_text, True, [long_text]),
            ("指令说明\n\n第二段", False, ["指令说明\n\n第二段"]),
        ]
        for text, is_llm, expected in cases:
            with self.subTest(text=text[:30]):
                event = self.event
                event.send = AsyncMock()
                result = event.plain_result(text).use_t2i(False)
                if is_llm:
                    result.set_result_content_type(ResultContentType.LLM_RESULT)
                event.set_result(result)
                async for _ in decorate.process(event):
                    pass
                await respond.process(event)
                actual = [call.args[0].get_plain_text() for call in event.send.await_args_list]
                self.assertEqual(actual, expected)

    def test_previous_topic_belongs_to_current_group_speaker(self):
        def turn(who, text):
            return {
                "role": "user",
                "content": [
                    {"type": "text", "text": text},
                    {"type": "text", "text": SPEAKER_PREFIX + json.dumps({"id": who})},
                ],
            }

        history = [
            turn("alice", "明天答辩"),
            turn("bob", "今天加班"),
            {"role": "user", "content": "没有说话者的历史"},
        ]
        query = retrieval_query("记得吗？", history, "alice", True)
        self.assertIn("明天答辩", query)
        self.assertNotIn("加班", query)
        self.assertNotIn("没有说话者", query)
        self.assertEqual(retrieval_query("新群", [], "alice", True), "新群")
        self.assertEqual(retrieval_query("你好", history, "carol", True), "你好")

    def test_friendship_guidance_needs_topic_and_retrieved_evidence(self):
        for ids, query in [(["L061"], "晚饭吃什么"), ([], "该原谅朋友吗")]:
            _, patterns, examples = compile_style(self.plugin.guide, ids, query)
            self.assertNotIn("B05", patterns)
            self.assertFalse({"E08", "E09"} & set(examples))


if __name__ == "__main__":
    unittest.main()
