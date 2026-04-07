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
                text = reminder.content
                if reminder.target_user_id:
                    from src.database.session import async_session_factory
                    from src.database.models import User
                    from sqlalchemy import select

                    async with async_session_factory() as session:
                        user_stmt = select(User).where(User.id == reminder.target_user_id)
                        user_result = await session.execute(user_stmt)
                        user = user_result.scalar_one_or_none()
                        if user:
                            mention = f"@{user.username}" if user.username else user.first_name or f"user_{reminder.target_user_id}"
                            text = f"{mention}, напоминаю: {reminder.content}"
                        else:
                            text = f"Напоминание: {reminder.content}"

                await bot.send_message(
                    chat_id=reminder.chat_id,
                    text=f"⏰ <b>Напоминание:</b>\n\n{text}",
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
    """Periodically update user profiles from memory (per-chat)."""

    async def _run() -> None:
        from src.database.models import UserProfile
        from sqlalchemy import select

        async for session in get_session():
            stmt = select(UserProfile.user_id, UserProfile.chat_id)
            result = await session.execute(stmt)
            profiles = list(result.fetchall())

            for user_id, chat_id in profiles:
                try:
                    # Profile is already updated per-chat in fact_extractor/relationship_engine
                    # This task is a no-op placeholder for future batch re-processing
                    logger.debug("Profile exists", user_id=user_id, chat_id=chat_id)
                except Exception:
                    logger.exception("Failed to update profile", user_id=user_id, chat_id=chat_id)

    asyncio.run(_run())
