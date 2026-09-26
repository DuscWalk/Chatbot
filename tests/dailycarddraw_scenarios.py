"""Exercise real command routing; backend responses and QQ sends are mocked."""

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


async def verify_dailycarddraw(plugin, rolebot, make_event):
    from astrbot.api.message_components import Image
    from astrbot.core.pipeline.process_stage.method.star_request import StarRequestSubStage
    from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
    from astrbot.core.platform.message_type import MessageType
    from data.plugins.astrbot_plugin_dailycarddraw.app.infrastructure.api_client import (
        DailyCardDrawApiClient,
    )

    cfg = copy.deepcopy(dict(plugin.context.get_config()))
    cfg.update(
        plugin_set=["astrbot_plugin_rolebot", "astrbot_plugin_dailycarddraw"],
        disable_builtin_commands=True,
        wake_prefix=["/"],
    )
    cfg["platform_settings"].update(unique_session=False, ignore_at_all=True)
    context = SimpleNamespace(astrbot_config=cfg)
    wake, dispatch = WakingCheckStage(), StarRequestSubStage()
    await wake.initialize(context)
    await dispatch.initialize(context)
    rolebot.config["groups"].update(all_groups=True, default_enabled=True)
    response = {
        "success": True,
        "data": {
            "record_no": "synthetic-record",
            "pool_name": "合成卡池",
            "draw_mode": "ten",
            "cards": [{"card_key": "synthetic", "card_name": "合成卡", "rarity": 6}],
            "image_url": "/api/daily-carddraw/images/synthetic.png",
            "quota": {"single_limit": 1, "ten_limit": 1, "ten_used": 1},
        },
    }
    with patch.object(DailyCardDrawApiClient, "_request", new_callable=AsyncMock) as api:
        api.return_value = response
        for index, (text, private) in enumerate(
            [
                ("/抽卡 十连", False),
                ("/十连寻访", True),
                ("/寻访 normal_pool", False),
                ("/抽卡帮助", False),
            ]
        ):
            ev = make_event(text)
            send = AsyncMock()
            ev.send = send
            if not private:
                ev.message_obj.type = MessageType.GROUP_MESSAGE
                ev.message_obj.group_id = f"synthetic-card-group-{index}"
                ev.session.message_type = MessageType.GROUP_MESSAGE
                ev.session.session_id = ev.message_obj.group_id
            await wake.process(ev)
            assert any(
                h.handler_module_path.endswith("astrbot_plugin_dailycarddraw.main")
                for h in ev.get_extra("activated_handlers", [])
            ), "daily-card command did not wake"
            results = []
            async for _ in dispatch.process(ev):
                if ev.get_result():
                    results.append(ev.get_result())
            assert ev.is_stopped(), "command would fall through to the chat model"
            assert len(results) == 1, "command produced duplicate replies or was swallowed"
            if "帮助" not in text:
                assert any(isinstance(c, Image) for c in results[0].chain)
                body = api.call_args.kwargs["json_body"]
                assert body["draw_mode"] == ("single" if index == 2 else "ten")
                assert bool(body["group_id"]) is (not private)
            send.assert_not_awaited()
        assert api.await_count == 3, "help must not contact the backend"
    return {"private_and_group_commands": True, "single_reply": True, "no_llm_fallback": True}
