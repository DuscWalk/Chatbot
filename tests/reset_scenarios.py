"""Exercise /reset through AstrBot's actual command dispatcher; no QQ sends."""

import asyncio
import copy
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


async def verify_reset_command(plugin, continuous, make_private, make_group):
    from astrbot.core.pipeline.context import PipelineContext
    from astrbot.core.pipeline.process_stage.method.star_request import StarRequestSubStage
    from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
    from astrbot.core.utils.active_event_registry import active_event_registry

    admin = "synthetic-reset-admin"
    plugin.config["reset_admin_ids"] = [admin]
    manager = plugin.context.conversation_manager
    memory = plugin.context.get_registered_star("astrbot_plugin_livingmemory").star_cls
    cfg = copy.deepcopy(dict(plugin.context.get_config()))
    cfg.update(wake_prefix=["/"], admins_id=["synthetic-native-admin"], plugin_set=["*"])
    cfg["platform_settings"].update(unique_session=False)
    ctx = PipelineContext(cfg, SimpleNamespace(context=plugin.context), "default")
    waking, dispatch = WakingCheckStage(), StarRequestSubStage()
    await waking.initialize(ctx)
    await dispatch.initialize(ctx)

    def group(text="/reset", sender=admin, gid="reset-group"):
        ev = make_group(text, sender, gid)
        ev.session.session_id = gid  # Production uses one shared conversation per group.
        return ev

    def private(text="/reset", sender=admin):
        ev = make_private(text, who=sender)
        ev.send = AsyncMock()
        return ev

    async def run(ev, *, builtins=True):
        waking.disable_builtin_commands = not builtins
        await waking.process(ev)
        handlers = ev.get_extra("activated_handlers")
        names = [handler.handler_name for handler in handlers]
        for later in ("route", "handle_private_msg", "reset"):
            if later in names:
                assert names.index("reset_chat") < names.index(later), names
        if builtins:
            assert any(h.handler_name == "reset" for h in handlers), "native collision untested"
        async for _ in dispatch.process(ev):
            pass
        assert ev.is_stopped(), "reset fell through to native command or LLM"

    async def seed(ev):
        return await manager.new_conversation(
            ev.unified_msg_origin,
            content=[{"role": "user", "content": "synthetic previous conversation"}],
            persona_id="蕾缪安",
        )

    async def history(ev, cid=None):
        cid = cid or await manager.get_curr_conversation_id(ev.unified_msg_origin)
        return await manager.get_conversation(ev.unified_msg_origin, cid)

    command, other_group, other_friend = (
        group(),
        group(gid="reset-other-group"),
        private(sender="reset-other-friend"),
    )
    cid, other_gid, other_fid = (
        await seed(command),
        await seed(other_group),
        await seed(other_friend),
    )
    scope, other_scope = plugin.group_scope(command), plugin.group_scope(other_group)
    for key in (scope, other_scope):
        plugin.group_context.add(
            key, "synthetic-message", "user", "合成用户", "旧群聊背景", time.time(), {}
        )
        plugin.policy.repeat(key, "synthetic-user", "synthetic-chain", time.time())
        plugin.policy.followups[(key, "synthetic-user")] = time.time() + 90
    plugin.pending_videos[command.unified_msg_origin] = (
        time.time() + 30,
        ["https://example.com/a.mp4"],
    )
    plugin.pending_videos[other_friend.unified_msg_origin] = (
        time.time() + 30,
        ["https://example.com/b.mp4"],
    )
    state = {"enabled": False, "muted_until": time.time() + 600, "probability": 0}
    await plugin.save_state(command, state)
    # A native AstrBot admin outside the allowlist may not use our reset in either scene.
    for ev in (
        group(sender="synthetic-native-admin"),
        group(sender="ordinary-member"),
        private(sender="synthetic-native-admin"),
    ):
        before = await manager.get_curr_conversation_id(ev.unified_msg_origin)
        sender = ev.send
        await run(ev)
        assert await manager.get_curr_conversation_id(ev.unified_msg_origin) == before
        assert (
            sender.await_count == 1 and "只有指定管理员" in sender.call_args.args[0].chain[0].text
        )
    assert json.loads((await history(command, cid)).history)
    assert scope in plugin.group_context.groups

    invalid = group("/reset another-group")
    await run(invalid)
    assert await manager.get_curr_conversation_id(command.unified_msg_origin) == cid
    assert json.loads((await history(command, cid)).history)

    old_request = group("旧问题", sender="another-member")
    old_request.set_extra("rolebot.capture_group", True)
    old_request.set_extra("rolebot.group_reply", "addressed")
    old_sender = old_request.send
    plugin.guard_send(old_request)
    other_active = group("另一个群的问题", gid="reset-other-group")
    for ev in (old_request, other_active, command):
        active_event_registry.register(ev)
    try:
        # Group profile disables native commands; off/mute/throttle must not block reset.
        sender = command.send
        await run(command, builtins=False)
        current = await history(command)
        assert current.cid != cid and json.loads(current.history) == []
        assert current.persona_id == "蕾缪安"
        assert json.loads((await history(command, cid)).history) == []
        assert old_request.is_stopped() and not other_active.is_stopped()
        assert scope not in plugin.group_context.groups
        assert scope not in plugin.policy.chains
        assert not any(k[0] == scope for k in plugin.policy.followups)
        assert command.unified_msg_origin not in plugin.pending_videos
        assert (await plugin.group_state(command)) == state
        assert sender.await_count == 1 and "已清空本群" in sender.call_args.args[0].chain[0].text
        # Late segments and after-send hooks from the old response must stay silent.
        await old_request.send(old_request.plain_result("旧回复的迟到分段"))
        old_sender.assert_not_awaited()
        await plugin.observe_group_reply(old_request)
        assert scope not in plugin.group_context.groups
        assert not any(k[0] == scope for k in plugin.policy.followups)
    finally:
        for ev in (old_request, other_active, command):
            active_event_registry.unregister(ev)
    assert (await history(other_group)).cid == other_gid
    assert (await history(other_friend)).cid == other_fid
    assert json.loads((await history(other_group)).history)
    assert json.loads((await history(other_friend)).history)
    assert other_scope in plugin.group_context.groups and other_scope in plugin.policy.chains
    assert other_friend.unified_msg_origin in plugin.pending_videos

    # Private reset clears a real pending debounce burst and the reflection buffer.
    command = private()
    private_cid = await seed(command)
    pending = private("这句还在等待合并")
    active_event_registry.register(pending)
    task = asyncio.create_task(continuous.handle_private_msg(pending))
    async with asyncio.timeout(2):
        while pending.unified_msg_origin not in continuous.sessions:
            await asyncio.sleep(0)
    reflection_manager = memory.event_handler.conversation_manager
    for ev in (command, other_friend):
        await reflection_manager.add_message(ev.unified_msg_origin, "user", "合成短期记忆素材")
        assert await reflection_manager.store.get_message_count(ev.unified_msg_origin) == 1
    clear = AsyncMock(wraps=reflection_manager.clear_session)

    try:
        sender = command.send
        with patch.object(reflection_manager, "clear_session", clear):
            await run(command)
        await asyncio.wait_for(task, timeout=2)
        assert pending.is_stopped()
        assert command.unified_msg_origin not in continuous.sessions
        clear.assert_awaited_once_with(command.unified_msg_origin)
        assert await reflection_manager.store.get_message_count(command.unified_msg_origin) == 0
        assert (
            await reflection_manager.store.get_message_count(other_friend.unified_msg_origin) == 1
        )
        current = await history(command)
        assert current.cid != private_cid and json.loads(current.history) == []
        assert current.persona_id == "蕾缪安"
        assert json.loads((await history(command, private_cid)).history) == []
        assert (
            sender.await_count == 1 and "已清空当前私聊" in sender.call_args.args[0].chain[0].text
        )
        assert json.loads((await history(other_friend)).history)
        assert json.loads((await history(other_group)).history)
    finally:
        active_event_registry.unregister(pending)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # Brand-new chats can be reset without a model or a previous conversation.
    fresh = group(gid="reset-never-used")
    await run(fresh, builtins=False)
    assert json.loads((await history(fresh)).history) == []
    assert len(fresh.get_extra("activated_handlers")) > 0

    # An internal failure must report failure and never fall through to native reset.
    failed = group(gid="reset-failure")
    failed_cid = await seed(failed)
    sender = failed.send
    with patch(
        "data.plugins.astrbot_plugin_rolebot.main.reset_current_chat",
        AsyncMock(side_effect=RuntimeError("synthetic reset failure")),
    ):
        await run(failed)
    assert (await history(failed)).cid == failed_cid
    assert json.loads((await history(failed)).history)
    assert sender.await_count == 1 and "未完成" in sender.call_args.args[0].chain[0].text

    # An unconfigured allowlist fails closed, including for native admins.
    plugin.config["reset_admin_ids"] = []
    denied = group(sender="synthetic-native-admin", gid="reset-failure")
    await run(denied)
    assert (await history(denied)).cid == failed_cid
    plugin.config["reset_admin_ids"] = [admin]

    # Other commands may stop propagation before AstrBot sends their result.
    ordinary_command = private("/普通指令")
    sender = ordinary_command.send
    plugin.guard_send(ordinary_command)
    ordinary_command.stop_event()
    await ordinary_command.send(ordinary_command.plain_result("正常命令回执"))
    sender.assert_awaited_once()
