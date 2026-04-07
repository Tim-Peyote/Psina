"""Skill registry — manages installed skills."""

from __future__ import annotations

import importlib
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.session import get_session
from src.database.models import Skill

logger = structlog.get_logger()


class SkillRegistry:
    """Register, install, and manage skills."""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}
        self._loaded_handlers: dict[str, object] = {}

    async def load_skills(self) -> None:
        """Load all active skills from database."""
        async for session in get_session():
            stmt = select(Skill).where(Skill.is_active == True)
            result = await session.execute(stmt)
            skills = list(result.scalars().all())

        self._skills = {s.slug: s for s in skills}
        logger.info("Skills loaded", count=len(self._skills))

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
        """Register a new skill in the database."""
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

        self._skills[slug] = skill
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

        self._skills.pop(slug, None)
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
            self._skills[slug] = skill
        return True

    def get_skill(self, slug: str) -> Skill | None:
        """Get a skill by slug."""
        return self._skills.get(slug)

    async def get_all_skills(self, include_inactive: bool = False) -> list[Skill]:
        """Get all skills."""
        async for session in get_session():
            stmt = select(Skill).order_by(Skill.name)
            if not include_inactive:
                stmt = stmt.where(Skill.is_active == True)
            result = await session.execute(stmt)
            return list(result.scalars().all())

        return []

    def get_skill_handler(self, slug: str) -> object | None:
        """Get the handler module for a skill.

        Handlers are expected at: src.skills.{slug}.handler
        """
        if slug in self._loaded_handlers:
            return self._loaded_handlers[slug]

        try:
            module = importlib.import_module(f"src.skills.{slug}.handler")
            self._loaded_handlers[slug] = module
            return module
        except ImportError:
            logger.debug("No handler module for skill", slug=slug)
            return None

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

        if slug in self._skills:
            self._skills[slug].config = config
        return True


# Singleton
skill_registry = SkillRegistry()
