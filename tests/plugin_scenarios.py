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
    VOICE_MARKER,
    needs_reference,
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
        self.voices = json.loads(
            (Path(__file__).resolve().parents[1] / "knowledge/lemuen/voice-lines.json").read_text()
        )["lines"]
        self.req = ProviderRequest(
            prompt="我是不是一定要原谅她？",
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
        self.assert_complete_voices(self.req.system_prompt)
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

    def assert_complete_voices(self, prompt):
        self.assertEqual(prompt.count(VOICE_MARKER), 1)
        self.assertEqual(len(self.voices), 38)
        for line in self.voices:
            self.assertIn(f"【{line['title']}】\n{line['text']}", prompt)

    async def test_native_request_preserved_and_reference_conditional(self):
        await self.plugin.on_request(self.event, self.req)
        self.assertIn("原生人格和其他系统设置", self.req.system_prompt)
        details = self.event.get_extra("lemuen")
        self.assertIn("L061", details["entry_ids"])
        self.assertEqual(self.req.prompt, "我是不是一定要原谅她？")
        marker = self.req.extra_user_content_parts[-1].text
        self.assertEqual(json.loads(marker.removeprefix(SPEAKER_PREFIX))["id"], "alice")
        await self.plugin.on_request(self.event, self.req)
        self.assertEqual(self.req.system_prompt.count(CONTEXT_MARKER), 1)
        self.assert_complete_voices(self.req.system_prompt)
        self.context.kb_manager.retrieve.assert_awaited_once()

    async def test_retrieval_failure_keeps_persona_without_friendship_examples(self):
        for error, status in [(TimeoutError(), "timeout"), (ValueError("private-data"), "error")]:
            req = ProviderRequest(system_prompt="原生人格")
            self.context.kb_manager.retrieve.side_effect = error
            await self.plugin.on_request(self.event, req)
            self.assertIn("原生人格", req.system_prompt)
            self.assertEqual(self.event.get_extra("lemuen")["retrieval"], status)
            self.assertNotIn("private-data", req.system_prompt)
            self.assert_complete_voices(req.system_prompt)

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
        self.assert_complete_voices(self.req.system_prompt)

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

    async def test_old_topic_is_not_retrieved_and_evidence_is_ephemeral(self):
        from astrbot.core.agent.message import Message, TextPart, dump_messages_with_checkpoints

        self.req.prompt = "奶酪小蛋糕"
        self.req.contexts = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "我是不是一定要原谅她？"},
                    {"type": "text", "text": SPEAKER_PREFIX + json.dumps({"id": "alice"})},
                ],
            }
        ]
        await self.plugin.on_request(self.event, self.req)
        self.context.kb_manager.retrieve.assert_not_awaited()
        self.assertEqual(self.event.get_extra("lemuen")["retrieval"], "not_needed")
        self.req = ProviderRequest(prompt="仙人掌挞你喜欢吗", contexts=self.req.contexts)
        await self.plugin.on_request(self.event, self.req)
        call = self.context.kb_manager.retrieve.await_args.kwargs
        self.assertEqual(call["query"], "仙人掌挞你喜欢吗")
        self.assertEqual(call["top_m_final"], 2)
        references = [
            p for p in self.req.extra_user_content_parts if p.text.startswith("[本轮原作")
        ]
        self.assertEqual(len(references), 1)
        self.assertIn("L061", references[0].text)
        self.assertNotIn("L061", self.req.system_prompt)
        saved = dump_messages_with_checkpoints(
            [
                Message(
                    role="user",
                    content=[TextPart(text=self.req.prompt), *self.req.extra_user_content_parts],
                )
            ]
        )
        self.assertNotIn("L061", str(saved))
        self.assertIn(SPEAKER_PREFIX, str(saved))

    def test_lore_gate_keeps_names_interests_and_does_not_trigger_on_address_alone(self):
        for query in [
            "安姐来一口",
            "我吃安姐吃剩的",
            "枢机，今天吃什么",
            "蕾缪安，你这句话没逻辑",
            "奶酪小蛋糕",
        ]:
            self.assertFalse(needs_reference(query, self.plugin.aliases), query)
        for query in [
            "你腿伤恢复得怎么样",
            "Sanctuary Inside 的歌词",
            "你会原谅安多恩吗",
            "仙人掌挞怎么样",
            "小乐最近怎么样",
            "你喜欢什么电影",
        ]:
            self.assertTrue(needs_reference(query, self.plugin.aliases), query)


if __name__ == "__main__":
    unittest.main()
