"""Ambient images exercise real event hooks without a QQ connection."""

from unittest.mock import AsyncMock, patch


async def verify_context_vision(plugin, group, image_url):
    from astrbot.api.message_components import Image, Plain
    from astrbot.api.provider import ProviderRequest
    from astrbot.core.agent.message import Message, TextPart, dump_messages_with_checkpoints
    from data.plugins.astrbot_plugin_rolebot.group_context import GroupContextBuffer
    from data.plugins.astrbot_plugin_rolebot.policy import GroupPolicy
    from data.plugins.astrbot_plugin_rolebot.vision.bridge import CONTEXT_VISION_PREFIX
    from data.plugins.astrbot_plugin_rolebot.vision.vision_pipeline import VisionPipelineResult

    plugin.group_context = GroupContextBuffer()
    plugin.policy = GroupPolicy()
    plugin.config["groups"].update(
        all_groups=True,
        default_enabled=True,
        context_enabled=True,
        probability=0,
        repeat_enabled=False,
        reply_cooldown_seconds=0,
        max_replies_per_minute=0,
    )
    serial = 0

    async def arrive(text, who, gid="vision-group", images=(), addressed=False):
        nonlocal serial
        serial += 1
        event = group(text, who, gid=gid, addressed=addressed)
        event.message_obj.message_id = f"vision-{serial}"
        event.message_obj.message = [
            Plain(text=text),
            *(Image(file=url, url=url) for url in images),
        ]
        await plugin.route(event)
        return event

    posted = await arrive("群友先吃", "alice", images=[image_url])
    assert posted.is_stopped(), "an ambient image must not force a reply"
    current = await arrive("安姐，这是什么", "bob", addressed=True)
    req = ProviderRequest(prompt=current.message_str)
    pipeline = await plugin.vision.ensure_pipeline()
    with patch.object(pipeline, "describe", wraps=pipeline.describe) as describe:
        await plugin.enrich(current, req)
        assert describe.await_count == 1, "text-only follow-up did not analyze ambient image"
        parts = [
            p for p in req.extra_user_content_parts if p.text.startswith(CONTEXT_VISION_PREFIX)
        ]
        assert len(parts) == 1 and "红" in parts[0].text and "蓝" in parts[0].text
        assert '"sender": "alice"' in parts[0].text and '"time":' in parts[0].text
        assert image_url not in parts[0].text and not req.image_urls
        saved = dump_messages_with_checkpoints(
            [
                Message(
                    role="user", content=[TextPart(text=req.prompt), *req.extra_user_content_parts]
                )
            ]
        )
        assert CONTEXT_VISION_PREFIX not in str(saved), "background image escaped its window"
        followup = await arrive("安姐，右边那个呢", "bob", addressed=True)
        await plugin.enrich(followup, ProviderRequest(prompt=followup.message_str))
        assert describe.await_count == 1, "same ambient image was analyzed again"
        other = await arrive("安姐，这是什么", "carol", gid="vision-other", addressed=True)
        other_req = ProviderRequest(prompt=other.message_str)
        await plugin.enrich(other, other_req)
        assert describe.await_count == 1 and not any(
            p.text.startswith(CONTEXT_VISION_PREFIX) for p in other_req.extra_user_content_parts
        ), "image crossed group boundaries"
        plugin.group_context.clear(plugin.group_scope(current))
        after_clear = await arrive("安姐，刚才的图", "bob", addressed=True)
        clear_req = ProviderRequest(prompt=after_clear.message_str)
        await plugin.enrich(after_clear, clear_req)
        assert describe.await_count == 1 and not any(
            p.text.startswith(CONTEXT_VISION_PREFIX) for p in clear_req.extra_user_content_parts
        ), "cleared image reused"

    # Failed/expired QQ media stays unknown, without exposing the download URL.
    secret_url = "https://example.com/signed-test-secret"
    await arrive("图", "alice", gid="vision-failure", images=[secret_url])
    failed = await arrive("安姐，这是什么", "bob", gid="vision-failure", addressed=True)
    failed_req = ProviderRequest(prompt=failed.message_str)
    unavailable = VisionPipelineResult(ok=False, context_text="", error="unavailable")
    with patch.object(pipeline, "describe", AsyncMock(return_value=unavailable)) as describe:
        await plugin.enrich(failed, failed_req)
        assert describe.await_count == 1
        output = "\n".join(p.text for p in failed_req.extra_user_content_parts)
        assert "尚未识别" in output and "红" not in output and secret_url not in output

    # Current images take the budget; failures and overflow keep native fallback inputs.
    direct = await arrive("安姐，这些呢", "bob", gid="vision-failure", addressed=True)
    refs = [f"https://example.com/current-{i}" for i in range(5)]
    direct_req = ProviderRequest(prompt=direct.message_str, image_urls=refs.copy())
    with patch.object(pipeline, "describe", AsyncMock(return_value=unavailable)) as describe:
        await plugin.enrich(direct, direct_req)
        assert describe.await_count == 1
        assert describe.await_args.args[0] == refs
        assert direct_req.image_urls == refs, "overflow images lost native fallback"
        output = "\n".join(p.text for p in direct_req.extra_user_content_parts)
        assert "未进入本轮识图" in output and secret_url not in output
