"""Feature integration in the disposable AstrBot lifecycle. Never connects QQ."""

import base64
import io
import json
from unittest.mock import AsyncMock, patch


async def verify_rolebot(plugin, continuous, vision, make_event, live):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain, Record, Reply
    from astrbot.api.provider import LLMResponse, ProviderRequest
    from astrbot.core.message.message_event_result import ResultContentType
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
        AiocqhttpMessageEvent,
    )
    from data.plugins.astrbot_plugin_rolebot.search import SearchEvidence
    from data.plugins.astrbot_plugin_rolebot.vision.vision_types import SearchSource
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (240, 160), "white")
    draw = ImageDraw.Draw(im)
    draw.rectangle((20, 40, 90, 110), fill="red")
    draw.ellipse((140, 40, 210, 110), fill="blue")
    buffer = io.BytesIO()
    im.save(buffer, format="PNG")
    image_url = "base64://" + base64.b64encode(buffer.getvalue()).decode()
    if not live:
        await verify_native_vision_options(image_url)
        vision.text_chat = AsyncMock(
            return_value=LLMResponse(
                role="assistant",
                completion_text=json.dumps(
                    {
                        "images": [
                            {
                                "image_number": 1,
                                "confidence": "no_identity",
                                "scene_description": "白色背景上有红色方形和蓝色圆形。",
                                "needs_web": False,
                                "needs_exact": False,
                            }
                        ],
                        "combined_answer": "",
                    },
                    ensure_ascii=False,
                ),
            )
        )
    pic = make_event("这是什么")
    await continuous._finalize_merged(pic, ["这是什么"], [image_url])
    assert "<image_caption>" not in pic.get_message_str(), "duplicate caption call"
    persona = "你有自己的朋友和生活；没有亲历过的事不说成自己的经历。"
    req = ProviderRequest(
        prompt=pic.get_message_str(), image_urls=[image_url], system_prompt=persona
    )
    await plugin.enrich(pic, req)
    descriptions = [p.text for p in req.extra_user_content_parts if p.text.startswith("[本轮图片")]
    assert not req.image_urls and descriptions, "image bridge not active"
    assert "红" in descriptions[0] and "蓝" in descriptions[0], "visual observations missing"
    assert req.system_prompt.startswith(persona), "image handling replaced the persona"
    assert "视觉证据处理" in req.system_prompt, "visual evidence rule missing"
    if not live:
        assert vision.text_chat.await_count == 1
    # A failed evidence model must leave images available to AstrBot's fallback.
    from data.plugins.astrbot_plugin_rolebot.vision.vision_pipeline import VisionPipelineResult
    from data.plugins.astrbot_plugin_rolebot.vision.vision_types import (
        ConfidenceBand,
        ImageDecision,
        VisionSynthesis,
    )

    failed = make_event("失败场景", mid="2")
    failed_req = ProviderRequest(prompt="失败场景", image_urls=[image_url])
    unavailable = VisionPipelineResult(
        ok=True,
        context_text="图片1：视觉信息暂不可用。",
        synthesis=VisionSynthesis((ImageDecision(1, ConfidenceBand.UNAVAILABLE),)),
    )
    with patch.object(plugin.vision.pipeline, "describe", AsyncMock(return_value=unavailable)):
        await plugin.enrich(failed, failed_req)
    assert failed_req.image_urls == [image_url]
    assert not failed_req.system_prompt, "failed analysis claimed visual context"
    cached = make_event("这是什么", mid="2")
    cached_req = ProviderRequest(prompt="这是什么", image_urls=[image_url])
    await plugin.enrich(cached, cached_req)
    if not live:
        assert vision.text_chat.await_count == 1, "image result cache missed"
    # Quoted image is already resolved by AstrBot; follow-ups read saved TextParts.
    quoted = make_event("这是什么", mid="3")
    quoted.message_obj.message = [Reply(id="synthetic-image", chain=[]), Plain(text="这是什么")]
    quoted_req = ProviderRequest(prompt="这是什么", image_urls=[image_url])
    await plugin.enrich(quoted, quoted_req)
    assert any("红" in p.text for p in quoted_req.extra_user_content_parts)
    unrelated = ProviderRequest(prompt="晚上好")
    await plugin.enrich(make_event("晚上好", who="other-friend", mid="4"), unrelated)
    assert not any(p.text.startswith("[本轮图片") for p in unrelated.extra_user_content_parts)
    assert not unrelated.system_prompt, "visual guidance leaked into unrelated chat"
    followup_req = ProviderRequest(
        prompt="右边那个是什么颜色？",
        system_prompt=persona,
        contexts=[{"role": "user", "content": [{"type": "text", "text": descriptions[0]}]}],
    )
    await plugin.enrich(make_event(followup_req.prompt, mid="40"), followup_req)
    assert "视觉证据处理" in followup_req.system_prompt
    assert followup_req.system_prompt.startswith(persona)
    assert not any(p.text.startswith("[本轮图片") for p in followup_req.extra_user_content_parts), (
        "follow-up invented a fresh image observation"
    )
    if not live:
        assert vision.text_chat.await_count == 1, "follow-up reanalyzed an absent image"

    if not live:
        mock_search = AsyncMock(
            return_value=SearchEvidence(
                "Synthetic web evidence",
                (SearchSource("Example", "https://example.com/source", "example.com"),),
                "ok",
            )
        )
        plugin.search._lookup = mock_search
    searched = ProviderRequest(prompt="搜索 AstrBot GitHub 最新 release 版本和发布时间")
    await plugin.enrich(make_event(searched.prompt, who="search-friend", mid="5"), searched)
    assert any("sources" in part.text for part in searched.extra_user_content_parts), (
        "search returned no verifiable sources"
    )
    if not live:
        mock_search.assert_awaited_once()
        for prompt in ["你现在心情怎么样", "恢复需要多长时间", "不要搜索，陪我聊两句"]:
            await plugin.enrich(make_event(prompt, mid="6"), ProviderRequest(prompt=prompt))
        mock_search.assert_awaited_once()

    def group(text, user, gid="synthetic-group", addressed=False):
        ev = make_event(text, who=user)
        ev.message_obj.type = MessageType.GROUP_MESSAGE
        ev.message_obj.group_id = gid
        # Use unique per-sender sessions to check group policy still shares group state.
        ev.session.message_type = MessageType.GROUP_MESSAGE
        ev.session.session_id = user + "_" + gid
        ev.is_at_or_wake_command = addressed
        ev.send = AsyncMock()
        return ev

    ignored = group("你好", "a", addressed=True)
    await plugin.route(ignored)
    assert ignored.is_stopped(), "groups must be opt-in"
    plugin.config["groups"]["whitelist"] = ["synthetic-group"]
    disabled = group("你好", "a", addressed=True)
    await plugin.route(disabled)
    assert disabled.is_stopped(), "whitelist alone must not enable group"
    command = group("bot on", "a", addressed=True)
    command.role = "admin"
    answers = [r async for r in plugin.control_group(command, "on")]
    assert answers and (await plugin.group_state(command))["enabled"]
    # These legacy scenarios exercise opt-in state at one timestamp.
    # Production throttles are covered separately below with a controlled clock.
    plugin.config["groups"].update(reply_cooldown_seconds=0, max_replies_per_minute=0)
    first = group("回声", "a")
    first_send = first.send
    await plugin.route(first)
    assert first.is_stopped() and first_send.await_count == 0
    same = group("回声", "a")
    same_send = same.send
    await plugin.route(same)
    assert same_send.await_count == 0, "one person cannot trigger repeat"
    second = group("回声", "b")
    second_send = second.send
    await plugin.route(second)
    assert second_send.await_count == 1 and second.is_stopped()
    assert second_send.call_args.args[0].chain[0].text == "回声"
    again = group("回声", "c")
    again_send = again.send
    await plugin.route(again)
    assert again_send.await_count == 0, "repeat cooldown"
    addressed = group("有空吗", "a", addressed=True)
    await plugin.route(addressed)
    quote_wake = group("这个呢", "q")
    quote_wake.message_obj.message = [
        Reply(id="old-bot-reply", sender_id="synthetic-bot"),
        Plain(text="这个呢"),
    ]
    await plugin.route(quote_wake)
    assert not quote_wake.is_stopped() and quote_wake.get_extra("rolebot.addressed")
    followup = group("那你呢？", "a")
    await plugin.route(followup)
    assert not followup.is_stopped()
    other = group("那你呢？", "b")
    await plugin.route(other)
    assert other.is_stopped(), "follow-up leaked to another sender"
    for operation in ("off", "on", "mute 10m"):
        _ = [r async for r in plugin.control_group(command, operation)]
    muted = group("蕾缪安", "a", addressed=True)
    await plugin.route(muted)
    assert muted.is_stopped()
    _ = [r async for r in plugin.control_group(command, "on")]
    _ = [r async for r in plugin.control_group(command, "clear")]

    # Quoting only once per sender per minute, including multi-segment replies.
    def llm_result(ev):
        ev.set_result(ev.plain_result("第一段。\n\n第二段。"))
        ev.get_result().result_content_type = ResultContentType.LLM_RESULT

    g1, g2 = group("hi", "quote-user"), group("hi", "quote-user")
    for ev in (g1, g2):
        llm_result(ev)
        await plugin.decorate(ev)
    assert sum(isinstance(c, Reply) for c in g1.get_result().chain) == 1
    assert not any(isinstance(c, Reply) for c in g2.get_result().chain)

    # Keep native segment ordering, but stop later sends after first failure.
    fail = make_event("hi")
    sending = AsyncMock(side_effect=[None, RuntimeError("synthetic send failure")])
    fail.send = sending
    plugin.guard_send(fail)
    for _ in range(4):
        await fail.send(fail.plain_result("synthetic chunk"))
    assert sending.await_count == 2

    # TTS generation failure falls back to the original character response.
    voice = make_event("发条语音")
    voice.set_extra("rolebot.voice", True)
    llm_result(voice)
    tts = type(
        "FakeTTS", (), {"get_audio": AsyncMock(side_effect=RuntimeError("synthetic TTS failure"))}
    )()
    with patch.object(plugin.context, "get_provider_by_id", return_value=tts):
        await plugin.decorate(voice)
    assert any(isinstance(c, Plain) for c in voice.get_result().chain)
    # Audio transmission failure also sends at most one text fallback.
    audio_event = make_event("语音")
    audio_event.set_extra("rolebot.voice_fallback", "文字回退")
    sending = AsyncMock(side_effect=[RuntimeError("audio failed"), None])
    audio_event.send = sending
    plugin.guard_send(audio_event)
    await audio_event.send(MessageChain([Record(file="fake.wav")]))
    await audio_event.send(audio_event.plain_result("不应发送"))
    assert sending.await_count == 2 and sending.call_args.args[0].chain[0].text == "文字回退"

    # Real AstrBot OneBot serialization must retain the metadata that Image drops.
    from data.plugins.astrbot_plugin_rolebot.media import OneBotMedia, repeat_payload

    typed = OneBotMedia("image", {"file": "base64://synthetic", "sub_type": 1, "summary": "[表情]"})
    encoded = await AiocqhttpMessageEvent._from_segment_to_dict(typed)
    assert encoded["data"]["sub_type"] == 1 and encoded["data"]["summary"] == "[表情]"
    unsafe = group("", "a")
    unsafe.message_obj.raw_message = {
        "message": [{"type": "mface", "data": {"emoji_id": "missing-metadata"}}]
    }
    assert repeat_payload(unsafe) == ("", None)
    unsafe.message_obj.raw_message = {
        "message": [{"type": "image", "data": {"url": "https://example.com/a.png"}}]
    }
    assert repeat_payload(unsafe) == ("", None)
    from group_scenarios import verify_group_burst, verify_group_context, verify_open_groups

    await verify_open_groups(plugin, group)
    await verify_group_context(plugin, group)
    await verify_group_burst(plugin, group, make_event)
    from reset_scenarios import verify_reset_command

    await verify_reset_command(plugin, continuous, make_event, group)
    return {
        "admin_context_reset": True,
        "group_burst_single_reply": True,
        "group_ambient_context": True,
        "all_group_routing": True,
        "group_throttle_and_controls": True,
        "vision": True,
        "quoted_image": True,
        "vision_cache": True,
        "search_sources": True,
        "group_opt_in": True,
        "repeat_and_cooldown": True,
        "followup_isolation": True,
        "quote_cooldown": True,
        "stop_after_send_failure": True,
        "voice_fallback": True,
        "onebot_media_metadata": True,
        "qq_sends": 0,
    }


async def verify_native_vision_options(image_url):
    """Exercise the real AstrBot adapter: kwargs alone silently lost this format."""
    import copy

    import httpx
    from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
    from data.plugins.astrbot_plugin_rolebot.vision.bridge import NativeAnalyzer
    from data.plugins.astrbot_plugin_rolebot.vision.image_preprocessor import ImagePreprocessor
    from data.plugins.astrbot_plugin_rolebot.vision.vision_types import (
        ImageLensResult,
        LensSearchResult,
    )
    from openai import AsyncOpenAI

    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "synthetic",
                "object": "chat.completion",
                "created": 1,
                "model": "synthetic-vision",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "images": [
                                        {
                                            "image_number": 1,
                                            "confidence": "no_identity",
                                            "question_answer": "两个色块。",
                                        }
                                    ],
                                    "combined_answer": "",
                                }
                            ),
                        },
                    }
                ],
            },
        )

    provider = ProviderOpenAIOfficial(
        {
            "id": "isolated",
            "type": "openai_chat_completion",
            "model": "synthetic-vision",
            "key": ["synthetic-key"],
            "api_base": "https://example.com/v1",
            "custom_extra_body": {"enable_thinking": False, "max_tokens": 128},
        },
        {},
    )
    await provider.client.close()
    provider.client = AsyncOpenAI(
        api_key="synthetic-key",
        base_url="https://example.com/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    before = copy.deepcopy(provider.provider_config)
    pre = ImagePreprocessor(
        timeout_seconds=1, max_download_bytes=100000, max_image_pixels=100000, allow_animation=True
    )
    image = await pre.fetch(image_url)
    try:
        result = await NativeAnalyzer(provider).synthesize(
            (image,),
            (ImageLensResult(1, LensSearchResult(ok=False)),),
            user_question="有几个色块",
            chat_context="",
            timeout_seconds=2,
        )
        assert "两个色块" in result.to_context_text()
        assert bodies[0]["response_format"]["type"] == "json_schema"
        assert (
            "question_answer"
            in bodies[0]["response_format"]["json_schema"]["schema"]["properties"]["images"][
                "items"
            ]["properties"]
        )
        assert bodies[0]["temperature"] == 0
        assert bodies[0]["max_tokens"] == 128 and bodies[0]["enable_thinking"] is False
        assert provider.provider_config == before, (
            "per-request format leaked into provider settings"
        )
    finally:
        await provider.client.close()
