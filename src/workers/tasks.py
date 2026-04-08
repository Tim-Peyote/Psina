import structlog
from celery import shared_task

from src.database.session import sync_session_factory
from src.database.models import Chat, ChatType, Reminder
from src.summarizer.daily import DailySummarizer
from src.memory_engine.engine import MemoryEngine
from src.workers.reminders import reminder_manager
from sqlalchemy import select

logger = structlog.get_logger()


def _run_async(coro):
    """Run async code in Celery with a fresh event loop."""
    import asyncio
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@shared_task(name="src.workers.tasks.generate_daily_summaries")
def generate_daily_summaries() -> None:
    """Generate daily summaries for all group chats."""
    import asyncio

    async def _run() -> None:
        summarizer = DailySummarizer()
        memory_engine = MemoryEngine()

        from src.database.session import get_async_session
        async for session in get_async_session():
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

    _run_async(_run())


@shared_task(name="src.workers.tasks.check_reminders")
def check_reminders() -> None:
    """Проверить и отправить pending напоминания."""
    from aiogram import Bot
    from src.config import settings
    from src.database.models import User

    # Use sync session for Celery compatibility
    with sync_session_factory() as session:
        # Get pending reminders
        stmt = select(Reminder).where(Reminder.is_sent == False)
        result = session.execute(stmt)
        pending = list(result.scalars().all())

    if not pending:
        return

    async def _send_reminders() -> None:
        bot = Bot(token=settings.telegram_bot_token)
        try:
            for reminder in pending:
                try:
                    text = reminder.content
                    if reminder.target_user_id:
                        with sync_session_factory() as session:
                            user_stmt = select(User).where(User.id == reminder.target_user_id)
                            user_result = session.execute(user_stmt)
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

                    # Mark as sent
                    with sync_session_factory() as session:
                        reminder.is_sent = True
                        session.add(reminder)
                        session.commit()

                    logger.info("Reminder sent", reminder_id=reminder.id, chat_id=reminder.chat_id)
                except Exception:
                    logger.exception("Failed to send reminder", reminder_id=reminder.id)
        finally:
            await bot.session.close()

    _run_async(_send_reminders())


@shared_task(name="src.workers.tasks.update_user_profiles")
def update_user_profiles() -> None:
    """Periodically update user profiles from memory (per-chat)."""
    from src.database.models import UserProfile
    from sqlalchemy import select

    with sync_session_factory() as session:
        stmt = select(UserProfile.user_id, UserProfile.chat_id)
        result = session.execute(stmt)
        profiles = list(result.fetchall())

        for user_id, chat_id in profiles:
            try:
                # Profile is already updated per-chat in fact_extractor/relationship_engine
                # This task is a no-op placeholder for future batch re-processing
                logger.debug("Profile exists", user_id=user_id, chat_id=chat_id)
            except Exception:
                logger.exception("Failed to update profile", user_id=user_id, chat_id=chat_id)
