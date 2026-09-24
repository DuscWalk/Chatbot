"""Offline integration of pinned plugins in a disposable AstrBot root, no QQ adapter."""

import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path
from unittest.mock import AsyncMock, patch

from manage_plugins import BUILD, ROOT, VISION_MODEL, defaults, extract, merge, read, write


async def verify(root, live=False):
    os.environ["ASTRBOT_ROOT"] = str(root)
    os.chdir(root)
    sys.path.insert(0, str(root))
    from astrbot.api.provider import LLMResponse, ProviderRequest
    from astrbot.core import LogBroker, astrbot_config, db_helper
    from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
    from astrbot.core.message.components import Plain
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.star.star_handler import EventType, star_handlers_registry

    astrbot_config.update(
        {
            "platform": [],
            "provider_sources": [
                {
                    "id": "synthetic",
                    "type": "openai_chat_completion",
                    "enable": True,
                    "api_base": "http://127.0.0.1:1/v1",
                    "key": ["synthetic-key"],
                }
            ],
            "provider": [
                {
                    "id": "deepseek/deepseek-v4-pro",
                    "provider_source_id": "synthetic",
                    "enable": True,
                    "model": "synthetic",
                },
                {
                    "id": "lemuen-vision",
                    "provider_source_id": "synthetic",
                    "enable": True,
                    "model": "synthetic-vision",
                },
                {
                    "id": "lemuen-embedding",
                    "type": "openai_embedding",
                    "enable": True,
                    "embedding_api_key": "synthetic-key",
                    "embedding_api_base": "http://127.0.0.1:1/v1",
                    "embedding_model": "synthetic-embedding",
                    "embedding_dimensions": 8,
                },
            ],
        }
    )
    astrbot_config["provider_settings"].update(
        {"default_provider_id": "deepseek/deepseek-v4-pro", "default_personality": "蕾缪安"}
    )
    if live:
        production = read(
            Path(os.environ.get("ASTRBOT_SMOKE_CONFIG", str(ROOT / "data/cmd_config.json")))
        )
        chat_config = next(
            p for p in production["provider"] if p["id"] == "deepseek/deepseek-v4-pro"
        )
        embedding = next(p for p in production["provider"] if p["id"] == "lemuen-embedding")
        chat_source = next(
            p
            for p in production["provider_sources"]
            if p["id"] == chat_config["provider_source_id"]
        )
        key = embedding["embedding_api_key"]
        vision_source = {
            "id": "synthetic-vision",
            "type": "openai_chat_completion",
            "enable": True,
            "api_base": embedding["embedding_api_base"],
            "key": [key] if isinstance(key, str) else key,
        }
        vision_config = {
            "id": "lemuen-vision",
            "provider_source_id": "synthetic-vision",
            "enable": True,
            "model": VISION_MODEL,
            "custom_extra_body": {"enable_thinking": False, "max_tokens": 2048},
        }
        astrbot_config.update(
            {
                "provider": [chat_config, embedding, vision_config],
                "provider_sources": [chat_source, vision_source],
            }
        )
    lock = read(ROOT / "plugins/lock.json")
    for item in lock["upstream"]:
        target = root / "data/plugins" / item["id"]
        extract((BUILD / (item["id"] + ".zip")).read_bytes(), target)
        cfg = merge(defaults(read(target / "_conf_schema.json")), item["config"])
        if "reflection_engine" in cfg:
            cfg["reflection_engine"]["summary_trigger_rounds"] = 2
        write(root / "data/config" / (item["id"] + "_config.json"), cfg)
    extract(
        (ROOT / "runtime/lemuen/build/astrbot_plugin_lemuen.zip").read_bytes(),
        root / "data/plugins",
        strip_root=False,
    )
    extract(
        (BUILD / "astrbot_plugin_rolebot.zip").read_bytes(), root / "data/plugins", strip_root=False
    )
    rolebot_config = defaults(read(root / "data/plugins/astrbot_plugin_rolebot/_conf_schema.json"))
    write(root / "data/config/astrbot_plugin_rolebot_config.json", rolebot_config)
    for name, content in lock["memory_prompts"].items():
        dest = root / "data/plugin_data/astrbot_plugin_livingmemory/prompts" / (name + ".txt")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
    lifecycle = AstrBotCoreLifecycle(LogBroker(), db_helper)
    await lifecycle.initialize()
    try:
        ctx = lifecycle.star_context
        assert not lifecycle.plugin_manager.failed_plugin_dict, "plugin failed to load"
        loaded = {m.name: m for m in ctx.get_all_stars() if not m.reserved}
        assert set(loaded) == {
            "astrbot_plugin_lemuen",
            "astrbot_plugin_rolebot",
            *(p["id"] for p in lock["upstream"]),
        }, list(loaded)
        assert all(p.meta().name == "webchat" for p in lifecycle.platform_manager.get_insts())
        await lifecycle.persona_mgr.create_persona(
            "蕾缪安", (ROOT / "knowledge/lemuen/persona.md").read_text(), tools=[], skills=[]
        )
        emb = ctx.get_provider_by_id("lemuen-embedding")
        chat = ctx.get_provider_by_id("deepseek/deepseek-v4-pro")
        vision = ctx.get_provider_by_id("lemuen-vision")
        if not live:
            emb.get_embedding = AsyncMock(return_value=[1.0] + [0.0] * 7)
            emb.get_embeddings = AsyncMock(
                side_effect=lambda texts, **kw: [[1.0] + [0.0] * 7 for _ in texts]
            )
            chat.text_chat = AsyncMock(
                return_value=LLMResponse(
                    role="assistant",
                    completion_text=json.dumps(
                        {
                            "summary": "合成博士喜欢桂花茶。",
                            "topics": ["桂花茶"],
                            "key_facts": ["合成博士喜欢桂花茶"],
                            "sentiment": "neutral",
                            "importance": 0.7,
                        },
                        ensure_ascii=False,
                    ),
                )
            )
            vision.text_chat = AsyncMock(
                return_value=LLMResponse(role="assistant", completion_text="合成图片上有一杯茶。")
            )
        memory = loaded["astrbot_plugin_livingmemory"].star_cls
        async with asyncio.timeout(40):
            while not memory.event_handler:
                await asyncio.sleep(0.1)

        def event(text, who="synthetic-doctor", mid="1"):
            msg = AstrBotMessage()
            msg.type = MessageType.FRIEND_MESSAGE
            msg.sender = MessageMember(user_id=who, nickname="合成博士")
            msg.message = [Plain(text=text)]
            msg.message_str = text
            msg.message_id = mid
            msg.raw_message = {}
            msg.self_id = "synthetic-bot"
            msg.timestamp = 1780000000 + int(mid)
            return AstrMessageEvent(
                text,
                msg,
                PlatformMetadata(name="aiocqhttp", id="napcat", description="synthetic"),
                who,
            )

        continuous = loaded["astrbot_plugin_continuous_message"].star_cls
        continuous.debounce_time = 0.1
        a, b, other = (
            event("第一句"),
            event("第二句", mid="2"),
            event("另一位的消息", "synthetic-other"),
        )
        first = asyncio.create_task(continuous.handle_private_msg(a))
        await asyncio.sleep(0.01)
        await continuous.handle_private_msg(b)
        await continuous.handle_private_msg(other)
        await first
        assert a.get_message_str() == "第一句\n第二句", a.get_message_str()
        assert b.is_stopped() and not a.is_stopped()
        assert other.get_message_str() == "另一位的消息"
        sys.path.insert(0, str(ROOT / "tests"))
        from rolebot_scenarios import verify_rolebot

        rolebot_results = await verify_rolebot(
            loaded["astrbot_plugin_rolebot"].star_cls, continuous, vision, event, live
        )
        for i, text in enumerate(["我喜欢桂花茶。", "记住我的这个喜好。"], 11):
            ev = event(text, mid=str(i))
            req = ProviderRequest(prompt=text)
            await memory.handle_memory_recall(ev, req)
            await memory.handle_memory_reflection(
                ev, LLMResponse(role="assistant", completion_text="知道了。")
            )
        if memory.event_handler._storage_tasks:
            await asyncio.gather(*list(memory.event_handler._storage_tasks))
        if not live:
            assert chat.text_chat.await_count >= 1, "reflection did not call model"
        recalled = ProviderRequest(prompt="我喜欢什么茶？")
        await memory.handle_memory_recall(event(recalled.prompt, mid="20"), recalled)
        assert any("桂花茶" in part.text for part in recalled.extra_user_content_parts), (
            "memory missing"
        )
        other_req = ProviderRequest(prompt="我喜欢什么茶？")
        await memory.handle_memory_recall(
            event(other_req.prompt, "synthetic-other", "21"), other_req
        )
        assert not other_req.extra_user_content_parts, "memory leaked to another friend"
        handlers = star_handlers_registry.get_handlers_by_event_type(
            EventType.OnLLMRequestEvent, plugins_name=["astrbot_plugin_lemuen"]
        )
        assert not any("livingmemory" in h.handler_module_path for h in handlers), (
            "group profile leak"
        )
        own = loaded["astrbot_plugin_lemuen"].star_cls
        own.config.update(
            {
                "enabled": True,
                "allowed_sessions": ["napcat:FriendMessage:*"],
                "proactive": {"enabled": True, "targets": [a.unified_msg_origin]},
            }
        )
        scheduler = own.proactive
        manager = ctx.conversation_manager
        cid = await manager.new_conversation(a.unified_msg_origin)
        conversation = await manager.get_conversation(a.unified_msg_origin, cid)
        scheduler.state[a.unified_msg_origin] = {"last_activity": 0}
        draft = {
            "cid": cid,
            "history": conversation.history,
            "text": "主动一句。\n\n另一个短句。",
            "activity": 0,
            "topic_index": 1,
        }
        sender = AsyncMock(return_value=True)
        with (
            patch.object(ctx, "send_message", sender),
            patch.object(scheduler, "still_idle", return_value=True),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            await scheduler.deliver(a.unified_msg_origin, draft)
        assert sender.await_count == 2
        saved = await manager.get_conversation(a.unified_msg_origin, cid)
        messages = json.loads(saved.history)
        assert len(messages) == 1 and messages[0]["role"] == "assistant", (
            "synthetic user trigger was saved"
        )
        if not live:
            chat.text_chat = AsyncMock(
                return_value=LLMResponse(
                    role="assistant", completion_text="博士，今天去了什么地方？"
                )
            )
        fake_platform = type(
            "SyntheticPlatform",
            (),
            {
                "meta": lambda self: PlatformMetadata(
                    name="aiocqhttp", id="napcat", description="synthetic"
                )
            },
        )()
        with patch.object(ctx, "get_platform_inst", return_value=fake_platform):
            generated = await scheduler.generate(a.unified_msg_origin)
        assert generated and generated["text"], "proactive generation failed"
        return {
            "live_api": live,
            "proactive_generation": True,
            "loaded": list(loaded),
            "debounce": True,
            "rolebot": rolebot_results,
            "memory_write_recall": True,
            "friend_isolation": True,
            "group_exclusion": True,
            "proactive_mock_delivery": True,
            "qq_sends": 0,
        }
    finally:
        for meta in lifecycle.star_context.get_all_stars():
            if meta.star_cls:
                await lifecycle.plugin_manager._terminate_plugin(meta)
        await lifecycle.platform_manager.terminate()
        await lifecycle.provider_manager.terminate()
        await lifecycle.kb_manager.terminate()
        await db_helper.engine.dispose()


def main():
    output = io.StringIO()
    with tempfile.TemporaryDirectory(prefix="plugins-check-") as folder:
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                result = asyncio.run(verify(Path(folder), live="--live" in sys.argv))
        except Exception:
            # Synthetic inputs and dummy credentials only; useful failure diagnostics.
            if "--live" not in sys.argv:
                sys.stderr.write(output.getvalue()[-12000:])
                if os.environ.get("GITHUB_ACTIONS") == "true":
                    diagnostic = output.getvalue()[-6000:] + traceback.format_exc()
                    escaped = (
                        diagnostic.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
                    )
                    print("::error title=Plugin integration::" + escaped, file=sys.stderr)
            raise
    write(ROOT / "runtime/plugins/latest-check.json", result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
