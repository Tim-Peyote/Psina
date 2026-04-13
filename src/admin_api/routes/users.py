from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from src.database.session import get_session
from src.database.models import User, UserProfile
from sqlalchemy import select, func

router = APIRouter(tags=["users"])


class UserInfo(BaseModel):
    id: int
    username: str | None
    first_name: str | None
    language_code: str | None
    created_at: str | None


class UserListResponse(BaseModel):
    users: list[UserInfo]
    total: int


class UserProfileResponse(BaseModel):
    user_id: int
    username: str | None
    first_name: str | None
    display_name: str | None
    traits: str | None
    interests: str | None
    summary: str | None


@router.get("/", response_model=UserListResponse, summary="List all users")
async def list_users(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> UserListResponse:
    """List users with pagination."""
    async for session in get_session():
        count_stmt = select(func.count(User.id))
        result = await session.execute(count_stmt)
        total = result.scalar() or 0

        stmt = select(User).order_by(User.updated_at.desc()).offset(offset).limit(limit)
        result = await session.execute(stmt)
        users = list(result.scalars().all())

        return UserListResponse(
            users=[
                UserInfo(
                    id=u.id,
                    username=u.username,
                    first_name=u.first_name,
                    language_code=u.language_code,
                    created_at=u.created_at.isoformat() if u.created_at else None,
                )
                for u in users
            ],
            total=total,
        )


@router.get("/{user_id}", response_model=UserProfileResponse, summary="Get user profile")
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
