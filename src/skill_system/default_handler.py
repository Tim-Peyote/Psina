"""Default handler — PromptOnlySkill.

Used when a skill has no custom handler.py.
Loads SKILL.md → enriches with context → passes to LLM → returns response.
"""

from __future__ import annotations

import structlog

from src.message_processor.processor import NormalizedMessage
from src.skill_system.state_manager import skill_state_manager
from src.llm_adapter.base import LLMProvider
from src.memory_services.context_pack import context_pack_builder
from src.orchestration_engine.emotional_state import emotional_state_manager

logger = structlog.get_logger()


async def process_message(
    skill,
    msg: NormalizedMessage,
    chat_id: int,
    user_id: int,
) -> str | None:
    """PromptOnlySkill: SKILL.md instructions + enriched context → LLM → response.

    This is the fallback handler for skills without custom logic.
    Now provides full context (user profile, memories, emotional state)
    so prompt-only skills can give contextual responses.
    """
    if not skill or not skill.full_content:
        return None

    # Load state
    state = await skill_state_manager.get_state(skill.slug, chat_id, default={})

    # Build enriched context pack
    context_pack = await context_pack_builder.build_context_pack(
        system_prompt=skill.full_content,
        chat_id=chat_id,
        user_id=user_id,
        query=msg.text,
        include_user_profile=True,
    )

    # Format messages from context pack
    messages = context_pack_builder.format_pack_for_llm(context_pack)

    # Add emotional state hint
    emo_state = await emotional_state_manager.get_state(chat_id)
    emo_hint = emo_state.get_prompt_hint()
    if emo_hint:
        messages.append({
            "role": "system",
            "content": f"Текущее эмоциональное состояние: {emo_hint}",
        })

    # Add skill state context if available
    if state:
        import json
        state_summary = json.dumps(state, ensure_ascii=False, default=str)
        if len(state_summary) < 500:
            messages.append({
                "role": "system",
                "content": f"Состояние скилла: {state_summary}",
            })

    llm = LLMProvider.get_provider()
    response = await llm.generate_response(messages=messages)

    await skill_state_manager.set_state(skill.slug, chat_id, state)

    logger.debug("PromptOnlySkill responded", skill=skill.slug)
    return response
