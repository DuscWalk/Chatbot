"""Reset one conversation using native managers and the pinned plugin APIs."""

import asyncio

from astrbot.core.utils.active_event_registry import active_event_registry


def active_plugin(context, name):
    metadata = context.get_registered_star(name)
    return metadata.star_cls if metadata and metadata.activated else None


async def reset_current_chat(plugin, event):
    umo = event.unified_msg_origin
    context = plugin.context
    manager = context.conversation_manager
    old_cid = await manager.get_curr_conversation_id(umo)
    old = await manager.get_conversation(umo, old_cid) if old_cid else None
    if old and old.user_id != umo:
        raise ValueError("Conversation scope mismatch")

    # Stop older requests (including a private message waiting for debounce).
    # A fresh conversation ID also invalidates pending proactive drafts.
    generation = plugin.context_generations.get(umo, 0) + 1
    plugin.context_generations[umo] = generation
    event.set_extra("rolebot.context_generation", generation)
    active_event_registry.stop_all(umo, exclude=event)
    if event.is_private_chat():
        continuous = active_plugin(context, "astrbot_plugin_continuous_message")
        pending = continuous.sessions.pop(umo, None) if continuous else None
        if pending:
            timer = pending.get("timer_task")
            if timer:
                timer.cancel()
            pending["flush_event"].set()
            # Let the old waiter exit before a new burst uses this session key.
            await asyncio.sleep(0)
        memory = active_plugin(context, "astrbot_plugin_livingmemory")
        if memory:
            if not memory.event_handler:
                raise RuntimeError("Memory plugin is not ready")
            # Clear only the reflection conversation, not extracted memories.
            await memory.event_handler.conversation_manager.clear_session(umo)
        lemuen = active_plugin(context, "astrbot_plugin_lemuen")
        if lemuen and lemuen.proactive:
            lemuen.proactive.activity(umo)

    await manager.new_conversation(umo, persona_id=old.persona_id if old else None)
    if old:
        await manager.update_conversation(umo, old_cid, history=[], token_usage=0)
    plugin.pending_videos.pop(umo, None)
    if not event.is_private_chat():
        scope = plugin.group_scope(event)
        plugin.group_context.clear(scope)
        plugin.policy.chains.pop(scope, None)
        plugin.policy.followups = {
            key: value for key, value in plugin.policy.followups.items() if key[0] != scope
        }
