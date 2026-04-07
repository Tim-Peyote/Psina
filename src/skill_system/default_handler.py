"""Default handler — PromptOnlySkill.

Used when a skill has no custom handler.py.
Loads SKILL.md → passes instructions to LLM → returns response.
"""

from __future__ import annotations

import structlog

from src.message_processor.processor import NormalizedMessage
from src.skill_system.state_manager import skill_state_manager
from src.llm_adapter.base import LLMProvider

logger = structlog.get_logger()


async def process_message(
    skill,
    msg: NormalizedMessage,
    chat_id: int,
    user_id: int,
) -> str | None:
    """PromptOnlySkill: SKILL.md instructions → LLM → response.

    This is the fallback handler for skills without custom logic.
    """
    if not skill or not skill.full_content:
        return None

    # Load state (some simple skills may not need it, but we save anyway)
    state = await skill_state_manager.get_state(skill.slug, chat_id, default={})

    llm = LLMProvider.get_provider()
    messages = [
        {"role": "system", "content": skill.full_content},
        {"role": "user", "content": msg.text},
    ]

    response = await llm.generate_response(messages=messages)

    await skill_state_manager.set_state(skill.slug, chat_id, state)

    logger.debug("PromptOnlySkill responded", skill=skill.slug)
    return response
