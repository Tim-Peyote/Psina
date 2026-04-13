"""Skill state manager — CRUD for per-chat skill state in database."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import structlog
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.session import get_session
from src.database.models import SkillState, SkillEvent

logger = structlog.get_logger()


class SkillStateManager:
    """Manage per-chat isolated state for each skill.

    Per-(skill_slug, chat_id) asyncio locks prevent lost-update races when
    multiple messages from the same chat are processed concurrently.
    """

    def __init__(self) -> None:
        # (skill_slug, chat_id) -> asyncio.Lock
        self._locks: dict[tuple[str, int], asyncio.Lock] = {}

    def _get_lock(self, skill_slug: str, chat_id: int) -> asyncio.Lock:
        key = (skill_slug, chat_id)
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    async def get_state(
        self,
        skill_slug: str,
        chat_id: int,
        default: dict | None = None,
    ) -> dict:
        """Get skill state for a specific chat."""
        async for session in get_session():
            stmt = select(SkillState).where(
                SkillState.skill_slug == skill_slug,
                SkillState.chat_id == chat_id,
            )
            result = await session.execute(stmt)
            record = result.scalar_one_or_none()

            if record:
                return record.state_json

            return default or {}

    async def set_state(
        self,
        skill_slug: str,
        chat_id: int,
        state: dict,
    ) -> None:
        """Upsert skill state for a chat."""
        async for session in get_session():
            stmt = select(SkillState).where(
                SkillState.skill_slug == skill_slug,
                SkillState.chat_id == chat_id,
            )
            result = await session.execute(stmt)
            record = result.scalar_one_or_none()

            if record:
                record.state_json = state
                record.last_activity_at = datetime.now(timezone.utc)
            else:
                session.add(
                    SkillState(
                        skill_slug=skill_slug,
                        chat_id=chat_id,
                        state_json=state,
                    )
                )

            await session.commit()

    async def update_field(
        self,
        skill_slug: str,
        chat_id: int,
        key: str,
        value,
    ) -> dict:
        """Update a single field in skill state (atomic get+set under lock)."""
        async with self._get_lock(skill_slug, chat_id):
            state = await self.get_state(skill_slug, chat_id, default={})
            state[key] = value
            await self.set_state(skill_slug, chat_id, state)
            return state

    def lock(self, skill_slug: str, chat_id: int):
        """Return asyncio.Lock for (skill_slug, chat_id). Use as async context manager
        when the caller needs to do its own atomic get+set sequence."""
        return self._get_lock(skill_slug, chat_id)

    async def delete_state(
        self,
        skill_slug: str,
        chat_id: int,
    ) -> bool:
        """Delete skill state for a chat."""
        async for session in get_session():
            stmt = select(SkillState).where(
                SkillState.skill_slug == skill_slug,
                SkillState.chat_id == chat_id,
            )
            result = await session.execute(stmt)
            record = result.scalar_one_or_none()

            if record:
                await session.delete(record)
                await session.commit()
                return True
            return False

    async def get_all_active_skills(self, chat_id: int) -> list[str]:
        """Get list of active skill slugs for a chat."""
        async for session in get_session():
            stmt = (
                select(SkillState.skill_slug)
                .where(
                    SkillState.chat_id == chat_id,
                    SkillState.is_active == True,
                )
            )
            result = await session.execute(stmt)
            return [row[0] for row in result.fetchall()]

    async def log_event(
        self,
        skill_slug: str,
        chat_id: int,
        event_type: str,
        content: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Log a skill event for audit trail."""
        async for session in get_session():
            session.add(
                SkillEvent(
                    skill_slug=skill_slug,
                    chat_id=chat_id,
                    event_type=event_type,
                    content=content,
                    event_metadata=metadata,
                )
            )
            await session.commit()


# Singleton
skill_state_manager = SkillStateManager()
