"""Low-frequency private conversation, using the selected persona and canon lookup."""

import asyncio
import contextlib
import json
import random
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .render import session_allowed

ZONE = ZoneInfo("Asia/Shanghai")
TOPICS = (
    "沿途见闻、博士今天去了什么样的地方",
    "音乐、最近听到的有趣旋律",
    "甜点、地方食物、各自的口味",
    "读书、故事、偶然遇见的新鲜事",
    "园艺、植物、生活中的小发现",
)
INSTRUCTION = """这一次由你自然发起一个小话题，不是在回答新问题。
结合下面的话题方向和真实聊天背景，写两三个简短段落，总计不超过180字。
保持蕾缪安的兴趣、分寸和打趣方式；不要固定问候模板、催回复或情绪关怀话术。
不虚构刚发生的行程、共同往事、现实新闻或与其他干员的新事件。
近期已经聊过的内容换个方向；不要重复上次主动消息。只输出要发出的聊天正文。"""


class ProactiveChat:
    def __init__(self, plugin, path: Path):
        self.plugin = plugin
        self.path = path
        self.boot_time = time.time()
        self.state = json.loads(path.read_text()) if path.exists() else {}
        self.task = None

    @property
    def config(self):
        return self.plugin.config.get("proactive", {})

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def allowed(self, umo):
        parts = umo.split(":")
        return (
            self.plugin.config.get("enabled", False)
            and self.config.get("enabled", False)
            and len(parts) == 3
            and parts[1] == "FriendMessage"
            and not any(c in umo for c in "*?[]")
            and umo in self.config.get("targets", [])
            and session_allowed(umo, self.plugin.config.get("allowed_sessions", []))
        )

    def activity(self, umo):
        if self.allowed(umo):
            self.state.setdefault(umo, {})["last_activity"] = time.time()
            self.save()

    def eligible(self, umo, now):
        if not self.allowed(umo):
            return False
        local = datetime.fromtimestamp(now, ZONE)
        start = max(9, min(20, int(self.config.get("start_hour", 19))))
        end = max(start + 1, min(22, int(self.config.get("end_hour", 21))))
        if local.isoweekday() not in [int(day) for day in self.config.get("weekdays", [1, 3, 6])]:
            return False
        if not start <= local.hour < end:
            return False
        record = self.state.setdefault(umo, {})
        day = local.date().isoformat()
        if record.get("attempt_day") == day:
            return False
        idle = max(30, int(self.config.get("idle_minutes", 120))) * 60
        if now - max(self.boot_time, record.get("last_activity", 0)) < idle:
            return False
        if record.get("schedule_day") != day:
            midnight = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            record.update(schedule_day=day, due=midnight + random.uniform(start, end) * 3600)
            self.save()
        return now >= record["due"]

    async def start(self):
        self.task = asyncio.create_task(self.loop())

    async def close(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

    async def loop(self):
        while True:
            await asyncio.sleep(60)
            for umo in list(self.config.get("targets", [])):
                try:
                    await self.tick(umo)
                except Exception as error:
                    # No recipient, chat content, API payload or credentials in logs.
                    self.plugin.logger.warning("Proactive chat skipped (%s).", type(error).__name__)

    async def tick(self, umo):
        if not self.eligible(umo, time.time()):
            return
        # One attempt per local day, persisted before generation or delivery. Never catch up.
        self.state[umo]["attempt_day"] = datetime.now(ZONE).date().isoformat()
        self.save()
        async with asyncio.timeout(120):
            draft = await self.generate(umo)
            if draft:
                await self.deliver(umo, draft)

    async def runtime_allowed(self, umo):
        from astrbot.api import sp

        cfg = self.plugin.context.get_config(umo)
        names = cfg.get("plugin_set", ["*"])
        if names != ["*"] and "astrbot_plugin_lemuen" not in names:
            return False
        if not cfg.get("provider_settings", {}).get("enable", True):
            return False
        services = await sp.get_async(
            scope="umo", scope_id=umo, key="session_service_config", default={}
        )
        if services.get("session_enabled") is False:
            return False
        plugins = await sp.get_async(
            scope="umo", scope_id=umo, key="session_plugin_config", default={}
        )
        return "astrbot_plugin_lemuen" not in plugins.get(umo, {}).get("disabled_plugins", [])

    async def generate(self, umo):
        from astrbot.api.provider import ProviderRequest
        from astrbot.core.platform.astr_message_event import AstrMessageEvent
        from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
        from astrbot.core.platform.message_type import MessageType

        context = self.plugin.context
        cfg = context.get_config(umo)
        if not await self.runtime_allowed(umo):
            return None
        manager = context.conversation_manager
        cid = await manager.get_curr_conversation_id(umo)
        conversation = await manager.get_conversation(umo, cid) if cid else None
        if not conversation:
            return None
        platform = context.get_platform_inst(umo.split(":")[0])
        if platform is None:
            return None
        selected, persona, _, _ = await context.persona_manager.resolve_selected_persona(
            umo=umo,
            conversation_persona_id=conversation.persona_id,
            platform_name=platform.meta().name,
            provider_settings=cfg.get("provider_settings", {}),
        )
        if selected != self.plugin.config.get("persona_id", "蕾缪安") or not persona:
            return None
        record = self.state[umo]
        activity = record.get("last_activity", 0)
        index = record.get("topic_index", 0) % len(TOPICS)
        topic = TOPICS[index]
        message = AstrBotMessage()
        message.type = MessageType.FRIEND_MESSAGE
        message.sender = MessageMember(user_id=umo.split(":")[2], nickname="博士")
        message.message = []
        message.message_str = topic
        message.raw_message = {}
        event = AstrMessageEvent(topic, message, platform.meta(), umo.split(":")[2])
        history = json.loads(conversation.history or "[]")
        contexts = [message for message in history if message.get("role") in {"user", "assistant"}]
        # The scheduling instruction is not a user utterance and is never saved to history.
        req = ProviderRequest(
            prompt=topic,
            system_prompt=persona["prompt"],
            contexts=contexts[-12:],
            conversation=conversation,
        )
        await self.plugin.on_request(event, req)
        if not event.get_extra("lemuen"):
            return None
        provider = await context.get_current_chat_provider_id(umo)
        response = await context.llm_generate(
            chat_provider_id=provider,
            prompt=f"{INSTRUCTION}\n话题方向：{topic}",
            system_prompt=req.system_prompt,
            contexts=req.contexts,
            tools=None,
        )
        text = (response.completion_text or "").strip()
        if response.role != "assistant" or not text or len(text) > 600:
            return None
        return {
            "text": text,
            "cid": cid,
            "history": conversation.history,
            "activity": activity,
            "topic_index": index + 1,
            "platform_name": platform.meta().name,
        }

    def still_idle(self, umo, draft):
        local = datetime.now(ZONE)
        return (
            self.allowed(umo)
            and local.isoweekday() in [int(day) for day in self.config.get("weekdays", [1, 3, 6])]
            and max(9, min(20, int(self.config.get("start_hour", 19)))) <= local.hour
            and local.hour < min(22, int(self.config.get("end_hour", 21)))
            and self.state[umo].get("last_activity", 0) == draft["activity"]
        )

    async def deliver(self, umo, draft):
        from astrbot.api.event import MessageChain
        from astrbot.core.utils.session_lock import session_lock_manager

        context = self.plugin.context
        manager = context.conversation_manager
        # Use the same lock as ordinary replies, so saved history cannot overwrite a new turn.
        async with session_lock_manager.acquire_lock(umo):
            if not self.still_idle(umo, draft) or not await self.runtime_allowed(umo):
                return
            if await manager.get_curr_conversation_id(umo) != draft["cid"]:
                return
            conversation = await manager.get_conversation(umo, draft["cid"])
            if not conversation or conversation.history != draft["history"]:
                return
            selected, _, _, _ = await context.persona_manager.resolve_selected_persona(
                umo=umo,
                conversation_persona_id=conversation.persona_id,
                platform_name=draft.get("platform_name", "aiocqhttp"),
                provider_settings=context.get_config(umo).get("provider_settings", {}),
            )
            if selected != self.plugin.config.get("persona_id", "蕾缪安"):
                return
            paragraphs = [p.strip() for p in draft["text"].split("\n\n") if p.strip()]
            if len(paragraphs) > 4:
                paragraphs = paragraphs[:3] + ["\n\n".join(paragraphs[3:])]
            sent = []
            try:
                for paragraph in paragraphs:
                    if not self.still_idle(umo, draft):
                        break
                    if not await context.send_message(umo, MessageChain().message(paragraph)):
                        break
                    sent.append(paragraph)
                    if len(sent) < len(paragraphs):
                        await asyncio.sleep(random.uniform(0.8, 1.8))
            finally:
                if sent:
                    history = json.loads(conversation.history or "[]")
                    history.append({"role": "assistant", "content": "\n\n".join(sent)})
                    await manager.update_conversation(umo, draft["cid"], history=history)
                    self.state[umo].update(topic_index=draft["topic_index"], last_sent=time.time())
                    self.save()
