"""Skill registry — manages installed skills.

Supports two loading modes:
1. File-based (SKILL.md): lazy discovery → activation
2. DB-based (legacy): full registration at startup
"""

from __future__ import annotations

import importlib
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.session import get_session
from src.database.models import Skill
from src.skill_system.skill_md_parser import discover_skill, activate_skill, SkillMetadata

logger = structlog.get_logger()

SKILLS_DIR = Path(__file__).parent.parent / "skills"


class SkillRegistry:
    """Register, install, and manage skills."""

    def __init__(self) -> None:
        # Discovery: name + description only (~50-100 tokens per skill)
        self._discovered: dict[str, SkillMetadata] = {}
        # Activation: full content loaded on demand
        self._activated: dict[str, SkillMetadata] = {}
        # DB-backed skills (legacy)
        self._db_skills: dict[str, Skill] = {}
        self._loaded_handlers: dict[str, object] = {}

    async def discover_skills(self) -> None:
        """Phase 1: Discovery — read only name+description from all SKILL.md files.

        This is cheap: ~50-100 tokens per skill.
        """
        if not SKILLS_DIR.exists():
            return

        for skill_dir in SKILLS_DIR.iterdir():
            if not skill_dir.is_dir():
                continue
            if skill_dir.name.startswith("_") or skill_dir.name.startswith("."):
                continue

            meta = discover_skill(skill_dir)
            if meta:
                self._discovered[meta.slug] = meta
                logger.debug("Skill discovered", slug=meta.slug, name=meta.name)

        logger.info("Skills discovered", count=len(self._discovered))

        # Sync file-based skills to DB so the router can find them
        await self._sync_file_skills_to_db()

        # Cleanup stale SkillState entries from old naming conventions
        await self._cleanup_stale_skill_state()

    async def _sync_file_skills_to_db(self) -> None:
        """Ensure all file-based skills are registered in the database."""
        for slug, meta in self._discovered.items():
            async for session in get_session():
                stmt = select(Skill).where(Skill.slug == slug)
                result = await session.execute(stmt)
                existing = result.scalar_one_or_none()

                if existing:
                    # Update if changed
                    existing.name = meta.name
                    existing.description = meta.description
                    existing.system_prompt = meta.full_content
                    await session.commit()
                    logger.debug("Skill synced in DB", slug=slug)
                else:
                    # Register new skill
                    skill = Skill(
                        slug=slug,
                        name=meta.name,
                        description=meta.description,
                        system_prompt=meta.full_content,
                        triggers=self._get_default_triggers(slug),
                        version="1.0.0",
                        config={},
                        is_active=True,
                    )
                    session.add(skill)
                    await session.commit()
                    logger.info("Skill registered in DB", slug=slug, name=meta.name)

        # Cleanup: remove stale slug entries from old naming convention
        # (e.g. 'agent-rpg' with dashes should be 'agent_rpg' with underscores)
        await self._cleanup_stale_slugs()

    async def _cleanup_stale_slugs(self) -> None:
        """Migrate stale skill entries from dash to underscore naming.

        e.g. 'agent-rpg' → 'agent_rpg'
        Preserves associated SkillState and SkillEvent data.
        """
        async for session in get_session():
            stmt = select(Skill)
            result = await session.execute(stmt)
            all_skills = list(result.scalars().all())

            slug_sets = {}
            for skill in all_skills:
                normalized = skill.slug.replace("-", "_")
                if normalized not in slug_sets:
                    slug_sets[normalized] = []
                slug_sets[normalized].append(skill)

            for normalized, skills in slug_sets.items():
                if len(skills) > 1:
                    # Find the dash version to remove
                    dash_skill = None
                    underscore_skill = None
                    for s in skills:
                        if "-" in s.slug:
                            dash_skill = s
                        else:
                            underscore_skill = s

                    if dash_skill and underscore_skill:
                        # Migrate any SkillState from dash → underscore
                        from src.database.models import SkillState, SkillEvent

                        # Move SkillState
                        stmt = select(SkillState).where(SkillState.skill_slug == dash_skill.slug)
                        result = await session.execute(stmt)
                        states = list(result.scalars().all())
                        for state in states:
                            state.skill_slug = underscore_skill.slug
                            logger.info("Migrated SkillState", from_slug=dash_skill.slug, to_slug=underscore_skill.slug)

                        # Move SkillEvent
                        stmt = select(SkillEvent).where(SkillEvent.skill_slug == dash_skill.slug)
                        result = await session.execute(stmt)
                        events = list(result.scalars().all())
                        for event in events:
                            event.skill_slug = underscore_skill.slug
                            logger.info("Migrated SkillEvent", from_slug=dash_skill.slug, to_slug=underscore_skill.slug)

                        # Now safe to delete the dash entry
                        logger.info("Removing stale skill entry", slug=dash_skill.slug)
                        await session.delete(dash_skill)

            await session.commit()

    async def _cleanup_stale_skill_state(self) -> None:
        """Deactivate ALL active SkillState entries at startup.

        This ensures users don't get stuck in broken/stale skill sessions
        from previous deployments. Users can re-activate skills when needed.
        """
        from src.database.models import SkillState

        async for session in get_session():
            stmt = select(SkillState).where(SkillState.is_active == True)
            result = await session.execute(stmt)
            active_states = list(result.scalars().all())

            if not active_states:
                return

            for state in active_states:
                state.is_active = False
                logger.info("Deactivated skill session at startup", slug=state.skill_slug, chat_id=state.chat_id)

            await session.commit()
            logger.info("All active skill sessions cleared at startup", count=len(active_states))

    def _get_default_triggers(self, slug: str) -> list[str]:
        """Default triggers for known skills."""
        trigger_map = {
            "agent_rpg": ["rpg", "играть", "игра", "давай сыграем", "dnd", "подземелья", "драконы", "ролевая", "сессия", "бросок", "кубик", "персонаж"],
        }
        return trigger_map.get(slug, [])

    async def activate_skill_by_slug(self, slug: str) -> SkillMetadata | None:
        """Phase 2: Activation — load full SKILL.md for a specific skill.

        This reads ~5000 tokens. Only called when skill is matched.
        """
        if slug in self._activated:
            return self._activated[slug]

        skill_path = SKILLS_DIR / slug.replace("-", "_")
        if not skill_path.exists():
            skill_path = SKILLS_DIR / slug
        if not skill_path.exists():
            return None

        meta = activate_skill(skill_path)
        if meta:
            self._activated[slug] = meta
            logger.info("Skill activated", slug=slug, tokens=len(meta.full_content))

        return meta

    def get_discovered(self) -> dict[str, SkillMetadata]:
        """Return all discovered skills (name + description only)."""
        return dict(self._discovered)

    def get_activated(self, slug: str) -> SkillMetadata | None:
        """Return activated skill with full content."""
        return self._activated.get(slug)

    def get_skill_description(self, slug: str) -> str:
        """Get description for LLM classification (cheap, no file read)."""
        if slug in self._discovered:
            return self._discovered[slug].description
        if slug in self._db_skills:
            return self._db_skills[slug].description
        return ""

    def get_all_descriptions(self) -> dict[str, str]:
        """Get all descriptions for LLM classification.

        Only name + description, not full content.
        """
        result = {}
        for slug, meta in self._discovered.items():
            result[slug] = meta.description
        for slug, skill in self._db_skills.items():
            result[slug] = skill.description
        return result

    async def load_db_skills(self) -> None:
        """Load DB-registered skills (legacy mode)."""
        async for session in get_session():
            stmt = select(Skill).where(Skill.is_active == True)
            result = await session.execute(stmt)
            skills = list(result.scalars().all())

        self._db_skills = {s.slug: s for s in skills}
        logger.info("DB skills loaded", count=len(self._db_skills))

    async def register_skill(
        self,
        slug: str,
        name: str,
        description: str,
        system_prompt: str,
        triggers: list[str] | None = None,
        version: str = "1.0.0",
        config: dict | None = None,
    ) -> Skill:
        """Register a new skill in the database (legacy)."""
        async for session in get_session():
            skill = Skill(
                slug=slug,
                name=name,
                description=description,
                system_prompt=system_prompt,
                triggers=triggers or [],
                version=version,
                config=config or {},
                is_active=True,
            )
            session.add(skill)
            await session.commit()
            await session.refresh(skill)

        self._db_skills[slug] = skill
        logger.info("Skill registered", slug=slug, name=name)
        return skill

    async def unregister_skill(self, slug: str) -> bool:
        """Remove a skill from the database."""
        async for session in get_session():
            stmt = select(Skill).where(Skill.slug == slug)
            result = await session.execute(stmt)
            skill = result.scalar_one_or_none()

            if not skill:
                return False

            await session.delete(skill)
            await session.commit()

        self._db_skills.pop(slug, None)
        self._loaded_handlers.pop(slug, None)
        logger.info("Skill unregistered", slug=slug)
        return True

    async def toggle_skill(self, slug: str, active: bool) -> bool:
        """Enable or disable a skill."""
        async for session in get_session():
            stmt = select(Skill).where(Skill.slug == slug)
            result = await session.execute(stmt)
            skill = result.scalar_one_or_none()

            if not skill:
                return False

            skill.is_active = active
            await session.commit()

        if skill:
            self._db_skills[slug] = skill
        return True

    def get_skill(self, slug: str) -> Skill | SkillMetadata | None:
        """Get a skill by slug."""
        return self._db_skills.get(slug) or self._discovered.get(slug)

    async def get_all_skills(self, include_inactive: bool = False) -> list[Skill]:
        """Get all DB skills."""
        async for session in get_session():
            stmt = select(Skill).order_by(Skill.name)
            if not include_inactive:
                stmt = stmt.where(Skill.is_active == True)
            result = await session.execute(stmt)
            return list(result.scalars().all())

        return []

    def get_skill_handler(self, slug: str) -> object | None:
        """Get the handler module for a skill.

        Priority:
        1. Custom handler.py in skill directory (ExecutableSkill)
        2. Default handler (PromptOnlySkill)

        This implements the split: prompt-only vs executable skills.
        """
        slug_underscored = slug.replace("-", "_")

        # Check for custom handler (ExecutableSkill)
        if slug_underscored in self._loaded_handlers:
            return self._loaded_handlers[slug_underscored]

        try:
            module = importlib.import_module(f"src.skills.{slug_underscored}.handler")
            self._loaded_handlers[slug_underscored] = module
            logger.debug("Loaded custom handler", slug=slug, mode="executable")
            return module
        except ImportError:
            pass

        # Fallback: PromptOnlySkill (default handler)
        from src.skill_system.default_handler import process_message as default_handler
        self._loaded_handlers[slug_underscored] = default_handler
        logger.debug("Using default handler", slug=slug, mode="prompt-only")
        return default_handler

    async def update_skill_config(self, slug: str, config: dict) -> bool:
        """Update skill configuration."""
        async for session in get_session():
            stmt = select(Skill).where(Skill.slug == slug)
            result = await session.execute(stmt)
            skill = result.scalar_one_or_none()

            if not skill:
                return False

            skill.config = config
            await session.commit()

        if slug in self._db_skills:
            self._db_skills[slug].config = config
        return True


# Singleton
skill_registry = SkillRegistry()
