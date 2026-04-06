import asyncio

import structlog
from celery import shared_task

from src.database.session import get_session
from src.database.models import Chat, ChatType
from src.summarizer.daily import DailySummarizer
from src.memory_engine.engine import MemoryEngine
from src.workers.reminders import reminder_manager

logger = structlog.get_logger()


@shared_task(name="src.workers.tasks.generate_daily_summaries")
def generate_daily_summaries() -> None:
    """Generate daily summaries for all group chats."""

    async def _run() -> None:
        summarizer = DailySummarizer()
        memory_engine = MemoryEngine()

        async for session in get_session():
            from sqlalchemy import select

            stmt = select(Chat).where(
                Chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP])
            )
            result = await session.execute(stmt)
            chats = list(result.scalars().all())

            for chat in chats:
                try:
                    summary = await summarizer.generate_summary(chat.id)
                    if summary:
                        logger.info("Summary generated", chat_id=chat.id)
                except Exception:
                    logger.exception("Failed to generate summary", chat_id=chat.id)

    asyncio.run(_run())


@shared_task(name="src.workers.tasks.check_reminders")
def check_reminders() -> None:
    """Проверить и отправить pending напоминания."""

    async def _run() -> None:
        from aiogram import Bot
        from src.config import settings

        pending = await reminder_manager.get_pending_reminders()
        if not pending:
            return

        bot = Bot(token=settings.telegram_bot_token)

        for reminder in pending:
            try:
                await bot.send_message(
                    chat_id=reminder.chat_id,
                    text=f"⏰ <b>Напоминание:</b>\n\n{reminder.content}",
                    parse_mode="HTML",
                )
                await reminder_manager.mark_sent(reminder.id)
                logger.info("Reminder sent", reminder_id=reminder.id, chat_id=reminder.chat_id)
            except Exception:
                logger.exception("Failed to send reminder", reminder_id=reminder.id)

        await bot.session.close()

    asyncio.run(_run())


@shared_task(name="src.workers.tasks.update_user_profiles")
def update_user_profiles() -> None:
    """Periodically update user profiles from memory."""

    async def _run() -> None:
        from src.database.models import UserProfile
        from sqlalchemy import select

        async for session in get_session():
            stmt = select(UserProfile)
            result = await session.execute(stmt)
            profiles = list(result.scalars().all())

            for profile in profiles:
                try:
                    logger.debug("Profile updated", user_id=profile.user_id)
                except Exception:
                    logger.exception("Failed to update profile", user_id=profile.user_id)

    asyncio.run(_run())
