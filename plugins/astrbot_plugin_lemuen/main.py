"""Use AstrBot's persona and knowledge base with conditional Lemuen references."""

import asyncio
import json
import time
from pathlib import Path

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart

from .render import (
    CONTEXT_MARKER,
    CONTEXT_RULE,
    SPEAKER_PREFIX,
    compile_style,
    entry_ids,
    native_chunks,
    retrieval_query,
    session_allowed,
)


class LemuenPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        guide = Path(__file__).with_name("voice-guide.json")
        if not guide.exists():  # Source checkout; release ZIP includes this asset.
            guide = Path(__file__).resolve().parents[2] / "knowledge/lemuen/voice-guide.json"
        self.guide = json.loads(guide.read_text(encoding="utf-8"))

    @filter.on_llm_request()
    async def on_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """Apply only to explicitly allowed sessions using the selected persona."""
        if not self.config.get("enabled", False):
            return
        if not session_allowed(event.unified_msg_origin, self.config.get("allowed_sessions", [])):
            return
        if CONTEXT_MARKER in (req.system_prompt or ""):
            return
        cfg = self.context.get_config(event.unified_msg_origin)
        selected, persona, _, _ = await self.context.persona_manager.resolve_selected_persona(
            umo=event.unified_msg_origin,
            conversation_persona_id=getattr(req.conversation, "persona_id", None),
            platform_name=event.get_platform_name(),
            provider_settings=cfg.get("provider_settings", {}),
        )
        if selected != self.config.get("persona_id", "蕾缪安"):
            return
        if persona and persona.get("tools") == []:
            # Honor an explicitly tool-free persona, including platform-added tools.
            req.func_tool = None
        prompt = event.get_message_str() or req.prompt or ""
        query = retrieval_query(
            prompt, req.contexts, event.get_sender_id(), bool(event.get_group_id())
        )
        kb_name = self.config.get("knowledge_base", "蕾缪安")
        chunks = native_chunks(req.extra_user_content_parts, kb_name)
        reused_native = bool(chunks)
        status = "native_context" if reused_native else "ok"
        started = time.monotonic()
        if not reused_native:
            try:
                timeout = max(1, min(30, int(self.config.get("retrieval_timeout_seconds", 8))))
                limit = max(1, min(10, int(self.config.get("top_k", 5))))
                async with asyncio.timeout(timeout):
                    result = await self.context.kb_manager.retrieve(
                        query=query, kb_names=[kb_name], top_k_fusion=20, top_m_final=limit
                    )
                chunks = [item["content"] for item in (result or {}).get("results", [])]
                if not chunks:
                    status = "empty"
            except Exception as error:
                # Keep provider payloads, keys, message text and QQ IDs out of logs.
                status = "timeout" if isinstance(error, TimeoutError) else "error"
                self.logger.warning("Lemuen retrieval %s; continuing with persona.", status)
                chunks = []
        # Keep complete chunks; oversized external documents never swallow the prompt.
        bounded, size = [], 0
        for chunk in chunks:
            if size + len(chunk) <= 16000:
                bounded.append(chunk)
                size += len(chunk)
        ids = entry_ids(bounded)
        style, patterns, examples = compile_style(self.guide, ids, query)
        knowledge = "" if reused_native else "\n".join(bounded)
        req.system_prompt = (req.system_prompt or "") + (
            f"\n{CONTEXT_MARKER}\n{CONTEXT_RULE}\n<参考资料>\n{knowledge}\n</参考资料>"
            f"\n<谈话方式>\n{style}\n</谈话方式>\n</lemuen_context>"
        )
        # This small, data-only annotation survives history saving; retrieved facts do not.
        speaker = {
            "id": event.get_sender_id(),
            "name": event.get_sender_name()[:80],
            "scene": "group" if event.get_group_id() else "private",
        }
        req.extra_user_content_parts.append(
            TextPart(text=SPEAKER_PREFIX + json.dumps(speaker, ensure_ascii=False))
        )
        event.set_extra(
            "lemuen",
            {
                "retrieval": status,
                "entry_ids": ids,
                "pattern_ids": patterns,
                "example_ids": examples,
                "retrieval_seconds": round(time.monotonic() - started, 3),
            },
        )
