"""Open-group routing in the disposable AstrBot lifecycle; all sends mocked."""

import copy
from types import SimpleNamespace
from unittest.mock import patch


async def verify_open_groups(plugin, group):
    from astrbot.api.message_components import At, Image, Plain, Reply
    from astrbot.api.provider import ProviderRequest
    from data.plugins.astrbot_plugin_rolebot.policy import GroupPolicy

    plugin.policy = GroupPolicy()
    plugin.config["groups"].update(
        all_groups=True,
        default_enabled=True,
        probability=0,
        blacklist=["blocked-group"],
        keywords=["蕾缪安", "安姐", "枢机", "拉特兰粉发"],
        reply_cooldown_seconds=3,
        max_replies_per_minute=6,
        repeat_window_seconds=45,
        random_cooldown_seconds=120,
    )
    # Patch only this module's clock, not asyncio/logging or the database's clock.
    with patch("data.plugins.astrbot_plugin_rolebot.main.time") as clock:

        async def route(text, user="new-user", gid="new-group", stamp=1000, **kwargs):
            clock.time.return_value = stamp
            ev = group(text, user, gid, **kwargs)
            await plugin.route(ev)
            return ev

        for index, name in enumerate(plugin.config["groups"]["keywords"]):
            ev = await route(name + "，晚上好", user=f"caller-{index}", stamp=1000 + index * 4)
            assert not ev.is_stopped() and ev.get_extra("rolebot.addressed"), name
        assert (await route("蕾缪安", gid="blocked-group")).is_stopped()
        assert not (await route("安姐", gid="future-group")).is_stopped()
        # The total budget applies even to @, while a new group has its own budget.
        assert (await route("你好", addressed=True, stamp=1013)).is_stopped()
        assert not (await route("你好", addressed=True, stamp=1015)).is_stopped()
        assert not (await route("你好", addressed=True, stamp=1018)).is_stopped()
        assert (await route("你好", addressed=True, stamp=1022)).is_stopped()
        assert not (await route("你好", addressed=True, stamp=1061)).is_stopped()

        # Commands remain available during reply cooldown, off, and mute.
        command = group("bot off", "admin", "new-group", addressed=True)
        command.role = "admin"
        await plugin.route(command)
        assert not command.is_stopped()
        _ = [answer async for answer in plugin.control_group(command, "off")]
        assert (await route("安姐", stamp=1200)).is_stopped()
        _ = [answer async for answer in plugin.control_group(command, "on")]
        _ = [answer async for answer in plugin.control_group(command, "mute 10m")]
        assert (await route("安姐", stamp=1210)).is_stopped()
        _ = [answer async for answer in plugin.control_group(command, "on")]
        unauthorized = group("bot off", "non-admin", "new-group", addressed=True)
        _ = [answer async for answer in plugin.control_group(unauthorized, "off")]
        assert (await plugin.group_state(command))["enabled"]

        # Follow-up belongs to one speaker, and the 90 seconds start after delivery.
        ev = await route("安姐", gid="follow-group", stamp=2000)
        clock.time.return_value = 2040
        await plugin.observe_group_reply(ev)
        assert not (await route("那你呢？", gid="follow-group", stamp=2129)).is_stopped()
        assert (await route("那你呢？", "stranger", "follow-group", 2140)).is_stopped()
        assert (await route("那你呢？", gid="follow-group", stamp=2220)).is_stopped()

        clock.time.return_value = 2300
        quote = group("怎么看", "a", "quote-group")
        quote.message_obj.message = [
            Reply(id="bot-message", sender_id="synthetic-bot"),
            Plain(text="怎么看"),
        ]
        await plugin.route(quote)
        assert not quote.is_stopped() and quote.get_extra("rolebot.addressed")

        # Random participation ignores messages addressed elsewhere and unsolicited media.
        plugin.config["groups"]["probability"] = 100
        for component in (
            At(qq="someone-else"),
            Reply(id="else", sender_id="someone-else"),
            Image(file="fake.png"),
        ):
            ev = group("帮我看看", "a", "random-group")
            ev.message_obj.message = [component, Plain(text="帮我看看")]
            sending = ev.send
            await plugin.route(ev)
            assert ev.is_stopped() and sending.await_count == 0
        assert not (await route("今晚天气不错", gid="random-group", stamp=2400)).is_stopped()
        assert (await route("今天出去散步", "other", "random-group", 2519)).is_stopped()
        random_ev = await route("窗外下雨了", "other", "random-group", 2520)
        assert not random_ev.is_stopped() and random_ev.get_extra("rolebot.group_reply") == "random"
        assert not random_ev.get_extra("rolebot.addressed")
        pic = group("安姐，这是什么", "a", "image-group")
        pic.message_obj.message = [Image(file="fake.png"), Plain(text=pic.message_str)]
        await plugin.route(pic)
        assert not pic.is_stopped() and pic.get_extra("rolebot.addressed")

        if hasattr(plugin.search._lookup, "await_count"):
            called = plugin.search._lookup.await_count
            ev = await route("枢机，搜索 AstrBot 最新版本", gid="search-group", stamp=2600)
            request = ProviderRequest(prompt=ev.message_str, system_prompt="persona")
            await plugin.enrich(ev, request)
            assert plugin.search._lookup.await_count == called + 1
            assert "这是群聊" in request.system_prompt
            assert any("sources" in part.text for part in request.extra_user_content_parts)

    # The native wake stage must still dispatch ordinary group messages to Rolebot,
    # keep a shared group session, and recognize its admin command with builtins off.
    from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
    from astrbot.core.umop_config_router import UmopConfigRouter

    router = UmopConfigRouter(None)
    router.umop_to_conf_id = {
        "napcat:GroupMessage:*": "groups",
        "napcat:FriendMessage:*": "friends",
    }
    assert router.get_conf_id_for_umop("napcat:GroupMessage:future") == "groups"
    assert router.get_conf_id_for_umop("napcat:FriendMessage:future") == "friends"
    cfg = copy.deepcopy(dict(plugin.context.get_config()))
    cfg.update(
        plugin_set=["astrbot_plugin_rolebot", "astrbot_plugin_lemuen"],
        disable_builtin_commands=True,
    )
    cfg["platform_settings"].update(unique_session=False, ignore_at_all=True)
    stage = WakingCheckStage()
    await stage.initialize(SimpleNamespace(astrbot_config=cfg))
    for text, addressed in (("安姐，晚上好", False), ("/bot status", True)):
        ev = group(text, "native-user", "native-group")
        ev.session.session_id = "native-group"
        await stage.process(ev)
        assert not ev.is_stopped() and ev.session.session_id == "native-group"
        assert bool(ev.is_at_or_wake_command) is addressed
        assert any(h.handler_name == "route" for h in ev.get_extra("activated_handlers"))
        if addressed:
            assert any(
                h.handler_name == "control_group" for h in ev.get_extra("activated_handlers")
            )


async def verify_group_context(plugin, group):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import At, Image, Plain
    from astrbot.api.provider import ProviderRequest
    from astrbot.core.agent.message import Message, TextPart, dump_messages_with_checkpoints
    from data.plugins.astrbot_plugin_rolebot.group_context import CONTEXT_PREFIX, GroupContextBuffer
    from data.plugins.astrbot_plugin_rolebot.policy import GroupPolicy

    plugin.group_context = GroupContextBuffer()
    plugin.policy = GroupPolicy()
    plugin.config["groups"].update(probability=0, repeat_enabled=False, context_enabled=True)
    serial = 0
    with patch("data.plugins.astrbot_plugin_rolebot.main.time") as clock:

        async def route(text, stamp, gid="ambient-group", components=None, addressed=False):
            nonlocal serial
            serial += 1
            clock.time.return_value = stamp
            ev = group(text, f"ambient-user-{serial}", gid, addressed=addressed)
            ev.message_obj.message_id = str(serial)
            if components is not None:
                ev.message_obj.message = components
            await plugin.route(ev)
            return ev

        # Ambient traffic passes through capture even though routing stops the event.
        for i in range(8):
            ev = await route(f"晚饭话题{i}", 1000 + i)
            assert ev.is_stopped()
        mentioned = await route("我也去", 1009, components=[At(qq="someone"), Plain(text="我也去")])
        assert mentioned.is_stopped()
        media = await route(
            "", 1010, components=[Image(file="private-path", url="https://example.com/secret")]
        )
        assert media.is_stopped()
        await route("另一个群", 1011, gid="ambient-other")
        current = await route("安姐，一起吗", 1012)
        assert not current.is_stopped()
        # New arrivals during generation cannot silently change this speaker's request.
        limited = await route("稍后到的新消息", 1013, addressed=True)
        assert limited.is_stopped(), "cooldown should still apply"
        req = ProviderRequest(
            prompt=current.message_str, contexts=[{"role": "assistant", "content": "历史回答"}]
        )
        await plugin.enrich(current, req)
        parts = [p for p in req.extra_user_content_parts if p.text.startswith(CONTEXT_PREFIX)]
        assert len(parts) == 1
        text = parts[0].text
        assert all(f"晚饭话题{i}" in text for i in range(8)), "time window was capped at six"
        assert "我也去" in text and "[图片]" in text
        assert not any(
            s in text
            for s in ["安姐，一起吗", "另一个群", "稍后到的新消息", "secret", "private-path"]
        )
        # AstrBot's real history serializer drops the ambient block.
        saved = dump_messages_with_checkpoints(
            [Message(role="user", content=[TextPart(text=req.prompt), *parts])]
        )
        assert saved[0]["content"] == [{"type": "text", "text": req.prompt}]
        assert req.contexts == [{"role": "assistant", "content": "历史回答"}]
        await plugin.enrich(current, req)
        assert sum(p.text.startswith(CONTEXT_PREFIX) for p in req.extra_user_content_parts) == 1

        clock.time.return_value = 1014
        await current.send(MessageChain([Plain(text="成功送达的回答")]))
        clock.time.return_value = 1015
        # Only successful sends join the ambient window.
        from unittest.mock import AsyncMock

        failed = group("安姐", "failed-user", "ambient-group", addressed=True)
        failed.message_obj.message_id = "failed-message"
        failed.send = AsyncMock(side_effect=RuntimeError("synthetic failure"))
        await plugin.route(failed)
        await failed.send(MessageChain([Plain(text="没有送达的回答")]))
        following = await route("安姐，还有呢", 1020)
        follow_req = ProviderRequest(prompt=following.message_str)
        await plugin.enrich(following, follow_req)
        next_text = next(
            p.text for p in follow_req.extra_user_content_parts if p.text.startswith(CONTEXT_PREFIX)
        )
        assert "稍后到的新消息" in next_text and "成功送达的回答" in next_text
        assert "没有送达的回答" not in next_text
        admin = group("bot clear", "admin", "ambient-group", addressed=True)
        admin.role = "admin"
        _ = [answer async for answer in plugin.control_group(admin, "clear")]
        assert plugin.group_scope(admin) not in plugin.group_context.groups
        _ = [answer async for answer in plugin.control_group(admin, "off")]
        await route("关闭期间的话", 1050)
        assert plugin.group_scope(admin) not in plugin.group_context.groups
        _ = [answer async for answer in plugin.control_group(admin, "on")]
        _ = [answer async for answer in plugin.control_group(admin, "mute 10m")]
        muted = await route("静默期间的话", 1060)
        assert muted.is_stopped() and plugin.group_scope(admin) in plugin.group_context.groups
        _ = [answer async for answer in plugin.control_group(admin, "on")]
        resumed = await route("安姐，回来了", 1070)
        assert "静默期间的话" in resumed.get_extra("rolebot.group_context")
        plugin.config["groups"]["context_enabled"] = False
        disabled = await route("安姐", 1080)
        assert not disabled.get_extra("rolebot.group_context")
        assert plugin.group_scope(admin) not in plugin.group_context.groups
