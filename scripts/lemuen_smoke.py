"""SSH worker: native AstrBot smoke run in a disposable root, with no QQ adapter."""

import asyncio
import base64
import contextlib
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import time
import traceback
import zipfile
from pathlib import Path

PHASE = "input"


async def run(request, root, production):
    global PHASE
    # Set before importing any AstrBot module: its globals create databases/configs.
    os.environ["ASTRBOT_ROOT"] = str(root)
    os.chdir(root)
    from astrbot.core import LogBroker, astrbot_config, db_helper
    from astrbot.core.astr_main_agent import MainAgentBuildConfig, build_main_agent
    from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
    from astrbot.core.message.components import Plain
    from astrbot.core.message.message_event_result import ResultContentType
    from astrbot.core.pipeline.context import PipelineContext, call_event_hook
    from astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal import (
        InternalAgentSubStage,
    )
    from astrbot.core.pipeline.respond.stage import RespondStage
    from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.star.star_handler import EventType

    PHASE = "select_provider"
    enabled = [
        p
        for p in production.get("provider", [])
        if p.get("enable", True) and p.get("provider_source_id")
    ]
    default = production.get("provider_settings", {}).get("default_provider_id")
    selected = [p for p in enabled if p.get("id") == default]
    if not selected and len(enabled) == 1:
        selected = enabled
    if len(selected) != 1:
        raise ValueError("ambiguous_chat_provider")
    chat = selected[0]
    source = next(
        s for s in production["provider_sources"] if s["id"] == chat["provider_source_id"]
    )
    if source.get("type") != "openai_chat_completion" or not source.get("enable", True):
        raise ValueError("unsupported_chat_source")
    chat["custom_extra_body"] = {
        **(chat.get("custom_extra_body") or {}),
        "max_tokens": 4096,
        "temperature": 0.6,
    }
    astrbot_config.update(
        {
            "platform": [],
            "provider": [chat, request["embedding"]],
            "provider_sources": [source],
            "kb_names": [],
            "kb_agentic_mode": False,
        }
    )
    astrbot_config["provider_settings"].update(
        {
            "default_provider_id": chat["id"],
            "default_personality": "蕾缪安",
            "identifier": False,
            "datetime_system_prompt": False,
            "request_max_retries": 1,
            "streaming_response": False,
            "computer_use_runtime": "none",
            "web_search": False,
        }
    )
    astrbot_config["provider_ltm_settings"]["group_message_history_enable"] = False
    for section, settings in request["chat_settings"].items():
        for key, value in settings.items():
            if isinstance(value, dict):
                astrbot_config[section][key].update(value)
            else:
                astrbot_config[section][key] = value
    plugin_path = root / "data/plugins"
    plugin_path.mkdir(parents=True, exist_ok=True)
    (root / "data/config").mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(request["plugin_zip"]))) as archive:
        for name in archive.namelist():
            if Path(name).is_absolute() or ".." in Path(name).parts:
                raise ValueError("invalid_plugin_archive")
        archive.extractall(plugin_path)
    PHASE = "initialize_astrbot"
    lifecycle = AstrBotCoreLifecycle(LogBroker(), db_helper)
    await lifecycle.initialize()
    try:
        if any(inst.meta().name != "webchat" for inst in lifecycle.platform_manager.get_insts()):
            raise AssertionError("unexpected_platform_adapter")
        plugin = lifecycle.star_context.get_registered_star("astrbot_plugin_lemuen")
        if not plugin or not plugin.star_cls:
            raise AssertionError(
                "plugin_not_loaded: "
                + str(
                    lifecycle.plugin_manager.failed_plugin_dict.get(
                        "astrbot_plugin_lemuen", {}
                    ).get("error", "not discovered")
                )
            )
        plugin.config.update(
            {"enabled": True, "allowed_sessions": [], "retrieval_timeout_seconds": 12}
        )
        PHASE = "create_persona_and_import_kb"
        await lifecycle.persona_mgr.create_persona(
            "蕾缪安", request["persona"], tools=[], skills=[]
        )
        kb = await lifecycle.kb_manager.create_kb(
            "蕾缪安",
            embedding_provider_id="lemuen-embedding",
            top_k_dense=20,
            top_k_sparse=20,
            top_m_final=5,
        )
        for document in request["knowledge"]["documents"]:
            await kb.upload_document(
                file_name=document["file_name"],
                file_content=None,
                file_type="md",
                pre_chunked_text=document["chunks"],
                batch_size=10,
                tasks_limit=1,
                max_retries=1,
            )
        await kb.refresh_kb()
        expected_chunks = sum(len(d["chunks"]) for d in request["knowledge"]["documents"])
        if kb.kb.chunk_count != expected_chunks:
            raise AssertionError("incomplete_import")
        PHASE = "native_retrieval"
        retrieval = []
        for case in request["retrieval_cases"]:
            result = await lifecycle.kb_manager.retrieve(
                case["question"], ["蕾缪安"], top_k_fusion=20, top_m_final=5
            )
            ids = [
                re.search(r"^# (L\d{3})\b", r["content"])[1]
                for r in (result or {}).get("results", [])
            ]
            retrieval.append(
                {
                    "case": case["id"],
                    "entry_ids": ids,
                    "hit": bool(set(ids) & set(case["expected_entry_ids"])),
                }
            )

        class NoSendEvent(AstrMessageEvent):
            async def send(self, message, **kwargs):
                # Capture native delivery only; no QQ adapter or actual send exists here.
                if not self.get_extra("preview_delivery"):
                    raise AssertionError("outbound_message_forbidden")
                self.get_extra("preview_messages").append(message.get_plain_text())

        pipeline = PipelineContext(astrbot_config, lifecycle.plugin_manager, "default")
        decorate = ResultDecorateStage()
        respond = RespondStage()
        await decorate.initialize(pipeline)
        await respond.initialize(pipeline)
        respond.interval = [0, 0]  # Exercise delivery without waiting between mock sends.
        replies = []
        save_stage = InternalAgentSubStage()
        save_stage.conv_manager = lifecycle.conversation_manager
        for case in request["dialogue_cases"]:
            for turn, item in enumerate(case["turns"], 1):
                PHASE = f"dialogue_{case['id']}_{turn}"
                if isinstance(item, str):
                    item = {"text": item, "sender_id": "doctor", "nickname": "博士"}
                msg = AstrBotMessage()
                msg.type = (
                    MessageType.GROUP_MESSAGE
                    if case["scene"] == "group"
                    else MessageType.FRIEND_MESSAGE
                )
                msg.sender = MessageMember(user_id=item["sender_id"], nickname=item["nickname"])
                msg.message = [Plain(text=item["text"])]
                msg.message_str = item["text"]
                msg.message_id = f"{case['id']}-{turn}"
                msg.group_id = case["id"] if case["scene"] == "group" else ""
                msg.self_id = "lemuen-preview"
                msg.raw_message = {}
                meta = PlatformMetadata(
                    name="aiocqhttp", description="isolated", id="lemuen-preview"
                )
                meta.support_proactive_message = False
                event = NoSendEvent(item["text"], msg, meta, case["id"])
                event.is_at_or_wake_command = True
                umo = event.unified_msg_origin
                if umo not in plugin.config["allowed_sessions"]:
                    plugin.config["allowed_sessions"].append(umo)
                started = time.monotonic()
                built = await build_main_agent(
                    event=event,
                    plugin_context=lifecycle.star_context,
                    config=MainAgentBuildConfig(
                        tool_call_timeout=30,
                        provider_settings=astrbot_config["provider_settings"],
                        kb_agentic_mode=False,
                        computer_use_runtime="none",
                        add_cron_tools=False,
                        streaming_response=False,
                        file_extract_enabled=False,
                    ),
                    apply_reset=False,
                )
                if not built:
                    raise AssertionError("agent_build_failed")
                req = built.provider_request
                try:
                    stopped = await call_event_hook(event, EventType.OnLLMRequestEvent, req)
                    if stopped or not event.get_extra("lemuen"):
                        raise AssertionError("plugin_hook_not_applied")
                    if req.func_tool and req.func_tool.tools:
                        raise AssertionError("unexpected_tools")
                except BaseException:
                    if built.reset_coro:
                        built.reset_coro.close()
                    raise
                await built.reset_coro
                async for _ in built.agent_runner.step_until_done(1):
                    pass
                answer = built.agent_runner.get_final_llm_resp()
                if not answer or not answer.completion_text:
                    raise AssertionError("empty_reply")
                await save_stage._save_to_history(
                    event,
                    req,
                    answer,
                    built.agent_runner.run_context.messages,
                    built.agent_runner.stats,
                )
                saved = await lifecycle.conversation_manager.get_conversation(
                    umo, req.conversation.cid
                )
                if "<lemuen_context>" in saved.history or "<参考资料>" in saved.history:
                    raise AssertionError("retrieval_persisted_in_history")
                if "[lemuen_speaker]" not in saved.history:
                    raise AssertionError("speaker_missing_from_history")
                event.set_extra("preview_delivery", True)
                event.set_extra("preview_messages", [])
                result = event.plain_result(answer.completion_text).use_t2i(False)
                result.set_result_content_type(ResultContentType.LLM_RESULT)
                event.set_result(result)
                async for _ in decorate.process(event):
                    pass
                await respond.process(event)
                messages = event.get_extra("preview_messages")
                if not messages:
                    raise AssertionError("empty_delivery")
                if re.sub(r"\s+", "", "".join(messages)) != re.sub(
                    r"\s+", "", answer.completion_text
                ):
                    raise AssertionError("delivery_text_changed")
                replies.append(
                    {
                        "case": case["id"],
                        "turn": turn,
                        "user": item["text"],
                        "reply": answer.completion_text,
                        "messages": messages,
                        **event.get_extra("lemuen"),
                        "seconds": round(time.monotonic() - started, 2),
                        "tokens": answer.usage.total if answer.usage else None,
                    }
                )
        return {
            "scope": "临时 AstrBot 根目录：原生插件加载、知识入库与检索、人格解析、请求钩子、Agent 回复、历史保存与模拟分段发送；无 QQ 适配器。",
            "model": chat["model"],
            "documents": len(request["knowledge"]["documents"]),
            "chunks": expected_chunks,
            "retrieval": retrieval,
            "replies": replies,
        }
    finally:
        for metadata in lifecycle.star_context.get_all_stars():
            if metadata.star_cls:
                await lifecycle.plugin_manager._terminate_plugin(metadata)
        await lifecycle.platform_manager.terminate()
        await lifecycle.provider_manager.terminate()
        await lifecycle.kb_manager.terminate()
        await db_helper.engine.dispose()


def main():
    global PHASE
    request = json.load(sys.stdin)
    project = Path(request["project"])
    config_path = project / "data/cmd_config.json"
    raw = config_path.read_bytes()
    before = hashlib.sha256(raw).hexdigest()
    production = json.loads(raw.decode("utf-8-sig"))
    os.umask(0o077)
    output = None
    try:
        with tempfile.TemporaryDirectory(prefix="lemuen-smoke-", dir=project / "runtime") as folder:
            # Upstream loggers can print initialization credentials: never return their output.
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                output = asyncio.run(run(request, Path(folder), production))
    except Exception as error:
        output = {
            "error": type(error).__name__,
            "phase": PHASE,
            "check": str(error) if isinstance(error, AssertionError) else None,
            "trace": [
                {"file": Path(f.filename).name, "line": f.lineno, "function": f.name}
                for f in traceback.extract_tb(error.__traceback__)[-8:]
            ],
        }
    output["production_unchanged"] = hashlib.sha256(config_path.read_bytes()).hexdigest() == before
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
