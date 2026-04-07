"""Register the RPG skill on startup."""

from __future__ import annotations

from src.skills.agent_rpg.system_prompt import (
    SKILL_NAME,
    SKILL_DISPLAY_NAME,
    SKILL_DESCRIPTION,
    SKILL_TRIGGERS,
    SYSTEM_PROMPT,
)
from src.skill_system.registry import skill_registry


async def register_rpg_skill() -> None:
    """Register the RPG skill if not already present."""
    existing = skill_registry.get_skill(SKILL_NAME)
    if existing:
        return  # Already registered

    await skill_registry.register_skill(
        slug=SKILL_NAME,
        name=SKILL_DISPLAY_NAME,
        description=SKILL_DESCRIPTION,
        system_prompt=SYSTEM_PROMPT,
        triggers=SKILL_TRIGGERS,
        version="1.0.0",
        config={
            "max_journal_entries": 50,
            "default_hp": 20,
        },
    )
