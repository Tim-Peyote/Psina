from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.database.session import get_session
from src.database.models import User, UserProfile
from sqlalchemy import select

router = APIRouter()


class UserProfileResponse(BaseModel):
    user_id: int
    username: str | None
    first_name: str | None
    display_name: str | None
    traits: str | None
    interests: str | None
    summary: str | None


@router.get("/{user_id}", response_model=UserProfileResponse)
async def get_user_profile(user_id: int, chat_id: int | None = None) -> UserProfileResponse:
    """Get user profile. If chat_id is provided, returns profile for that specific chat."""
    async for session in get_session():
        user_stmt = select(User).where(User.id == user_id)
        user_result = await session.execute(user_stmt)
        user = user_result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        profile_stmt = select(UserProfile)
        if chat_id:
            profile_stmt = profile_stmt.where(
                UserProfile.user_id == user_id,
                UserProfile.chat_id == chat_id,
            )
        else:
            profile_stmt = profile_stmt.where(UserProfile.user_id == user_id)
        profile_result = await session.execute(profile_stmt)
        profile = profile_result.scalar_one_or_none()

        return UserProfileResponse(
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            display_name=profile.display_name if profile else None,
            traits=profile.traits if profile else None,
            interests=profile.interests if profile else None,
            summary=profile.summary if profile else None,
        )


@router.get("/")
async def list_users(limit: int = 50) -> list[dict]:
    async for session in get_session():
        stmt = select(User).order_by(User.updated_at.desc()).limit(limit)
        result = await session.execute(stmt)
        users = list(result.scalars().all())
        return [
            {
                "id": u.id,
                "username": u.username,
                "first_name": u.first_name,
                "language_code": u.language_code,
                "created_at": str(u.created_at),
            }
            for u in users
        ]
