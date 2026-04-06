from datetime import datetime, timezone

from fastapi import APIRouter, Query
from pydantic import BaseModel

from src.database.session import get_session
from src.database.models import UsageStat
from sqlalchemy import select

router = APIRouter()


class UsageStatResponse(BaseModel):
    date: datetime
    provider: str
    model: str
    tokens_prompt: int
    tokens_completion: int
    requests_count: int


@router.get("/", response_model=list[UsageStatResponse])
async def get_usage_stats(
    days: int = Query(7, ge=1, le=90),
) -> list[UsageStatResponse]:
    async for session in get_session():
        stmt = (
            select(UsageStat)
            .order_by(UsageStat.date.desc())
            .limit(days)
        )
        result = await session.execute(stmt)
        stats = list(result.scalars().all())
        return [
            UsageStatResponse(
                date=s.date,
                provider=s.provider,
                model=s.model,
                tokens_prompt=s.tokens_prompt,
                tokens_completion=s.tokens_completion,
                requests_count=s.requests_count,
            )
            for s in stats
        ]
