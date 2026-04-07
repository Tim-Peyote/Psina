"""Register the RPG skill on startup (legacy DB registration).

Note: SKILL.md-based skills are auto-discovered at startup.
This is kept for backward compatibility with DB-based skills.
"""

from __future__ import annotations

from pathlib import Path

from src.skill_system.skill_md_parser import discover_skill

SKILLS_DIR = Path(__file__).parent.parent
SKILL_DIR = SKILLS_DIR / "agent_rpg"


async def register_rpg_skill() -> None:
    """No-op — SKILL.md is auto-discovered by skill_registry.discover_skills().

    Kept for backward compatibility with main.py startup sequence.
    """
    meta = discover_skill(SKILL_DIR)
    if meta:
        from src.skill_system.registry import skill_registry
        from structlog import get_logger
        logger = get_logger()
        logger.info("RPG skill auto-discovered via SKILL.md", slug=meta.slug)
