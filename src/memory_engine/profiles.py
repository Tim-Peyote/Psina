"""User profile management."""

import json

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.session import get_session
from src.database.models import UserProfile

logger = structlog.get_logger()


async def update_profile(
    user_id: int,
    traits: list[str] | None = None,
    interests: list[str] | None = None,
    summary: str | None = None,
) -> UserProfile:
    """Update or create a user profile."""
    async for session in get_session():
        stmt = select(UserProfile).where(UserProfile.user_id == user_id)
        result = await session.execute(stmt)
        profile = result.scalar_one_or_none()

        if profile is None:
            profile = UserProfile(user_id=user_id)
            session.add(profile)

        if traits:
            existing = json.loads(profile.traits) if profile.traits else []
            merged = _merge_lists(existing, traits)
            profile.traits = json.dumps(merged)

        if interests:
            existing = json.loads(profile.interests) if profile.interests else []
            merged = _merge_lists(existing, interests)
            profile.interests = json.dumps(merged)

        if summary:
            profile.summary = summary

        await session.commit()
        await session.refresh(profile)
        return profile


async def get_or_create_profile(user_id: int) -> UserProfile:
    """Get or create a user profile."""
    async for session in get_session():
        stmt = select(UserProfile).where(UserProfile.user_id == user_id)
        result = await session.execute(stmt)
        profile = result.scalar_one_or_none()

        if profile is None:
            profile = UserProfile(user_id=user_id)
            session.add(profile)
            await session.commit()
            await session.refresh(profile)

        return profile


def _merge_lists(existing: list[str], new: list[str], max_items: int = 50) -> list[str]:
    """Merge two lists, avoiding duplicates."""
    seen = set(existing)
    merged = list(existing)
    for item in new:
        if item not in seen:
            merged.append(item)
            seen.add(item)
    return merged[:max_items]
