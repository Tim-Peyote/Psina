from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.session import get_session
from src.database.models import Summary, Message
from src.llm_adapter.base import LLMProvider

logger = structlog.get_logger()


class DailySummarizer:
    """Generates daily summaries for chats."""

    async def generate_summary(self, chat_id: int) -> Summary | None:
        """Generate a daily summary for a chat."""
        yesterday = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

        # Check if summary already exists
        async for session in get_session():
            stmt = select(Summary).where(
                and_(Summary.chat_id == chat_id, Summary.date == yesterday)
            )
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()
            if existing:
                return existing

            # Get yesterday's messages
            msg_stmt = (
                select(Message)
                .where(
                    and_(
                        Message.chat_id == chat_id,
                        Message.created_at >= yesterday,
                        Message.created_at < today,
                    )
                )
                .order_by(Message.created_at)
            )
            msg_result = await session.execute(msg_stmt)
            messages = list(msg_result.scalars().all())

            if not messages:
                return None

            texts = [m.text for m in messages]
            llm = LLMProvider.get_provider()
            content = await llm.summarize(texts, max_tokens=500)

            summary = Summary(
                chat_id=chat_id,
                date=yesterday,
                content=content,
            )
            session.add(summary)
            await session.commit()
            await session.refresh(summary)

            logger.info("Daily summary generated", chat_id=chat_id, messages=len(messages))
            return summary

    async def get_latest_summary(self, chat_id: int) -> Summary | None:
        """Get the most recent summary for a chat."""
        async for session in get_session():
            stmt = (
                select(Summary)
                .where(Summary.chat_id == chat_id)
                .order_by(Summary.date.desc())
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()
