"""Token budget tracking and enforcement."""

from datetime import datetime, timezone

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.database.session import get_session
from src.database.models import UsageStat

logger = structlog.get_logger()


class TokenBudget:
    """Track and enforce daily token budgets."""

    async def can_use_tokens(self, estimated_tokens: int) -> bool:
        """Check if we're within budget."""
        usage = await self._get_today_usage()
        total_used = usage.tokens_prompt + usage.tokens_completion
        return (total_used + estimated_tokens) <= settings.daily_token_budget

    async def record_usage(self, tokens_prompt: int, tokens_completion: int) -> None:
        """Record token usage."""
        async for session in get_session():
            today = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            )

            stmt = select(UsageStat).where(
                UsageStat.date == today,
                UsageStat.provider == settings.llm_provider,
                UsageStat.model == settings.llm_model,
            )
            result = await session.execute(stmt)
            stat = result.scalar_one_or_none()

            if stat:
                stat.tokens_prompt += tokens_prompt
                stat.tokens_completion += tokens_completion
                stat.requests_count += 1
            else:
                stat = UsageStat(
                    date=today,
                    provider=settings.llm_provider,
                    model=settings.llm_model,
                    tokens_prompt=tokens_prompt,
                    tokens_completion=tokens_completion,
                    requests_count=1,
                )
                session.add(stat)

            await session.commit()

    async def get_remaining(self) -> int:
        """Get remaining tokens for today."""
        usage = await self._get_today_usage()
        total_used = usage.tokens_prompt + usage.tokens_completion
        return max(0, settings.daily_token_budget - total_used)

    async def _get_today_usage(self) -> UsageStat:
        """Get or create today's usage stat."""
        today = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        async for session in get_session():
            stmt = select(UsageStat).where(
                UsageStat.date == today,
                UsageStat.provider == settings.llm_provider,
                UsageStat.model == settings.llm_model,
            )
            result = await session.execute(stmt)
            stat = result.scalar_one_or_none()

            if stat:
                return stat

            stat = UsageStat(
                date=today,
                provider=settings.llm_provider,
                model=settings.llm_model,
            )
            session.add(stat)
            await session.commit()
            return stat


token_budget = TokenBudget()
