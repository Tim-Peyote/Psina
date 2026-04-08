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
    """Run async code in Celery with a fresh event loop and isolated DB engine.

    Celery prefork forks create new processes without event loops.
    The module-level async engine is bound to the old (non-existent) loop.
    We create a fresh engine inside the new loop to avoid 'different loop' errors.
    """
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        # Recreate the async engine inside this loop so asyncpg connections
        # are bound to the correct event loop
        from src.database import session as db_session
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        from src.config import settings

        db_session.engine = create_async_engine(
            settings.database_url,
            echo=False,
            pool_size=2,
            max_overflow=5,
            pool_pre_ping=True,
            pool_recycle=3600,
        )
        db_session.async_session_factory = async_sessionmaker(
            db_session.engine,
            class_=db_session.AsyncSession,
            expire_on_commit=False,
        )

        result = loop.run_until_complete(coro)

        # Cleanup: close the engine
        async def _close():
            await db_session.engine.dispose()
        loop.run_until_complete(_close())

        return result
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
    from datetime import datetime, timezone

    # Use sync session for Celery compatibility
    with sync_session_factory() as session:
        # Get ALL unsent reminders (don't filter by time in SQL to avoid tz issues)
        stmt = select(Reminder).where(Reminder.is_sent == False)
        result = session.execute(stmt)
        pending = list(result.scalars().all())

    # Timezone-safe filter in Python
    now = datetime.now(timezone.utc)
    due = []
    for r in pending:
        if r.remind_at is None:
            continue
        # Make remind_at timezone-aware if it isn't
        remind_at = r.remind_at
        if remind_at.tzinfo is None:
            remind_at = remind_at.replace(tzinfo=timezone.utc)
        if remind_at <= now:
            due.append(r)

    if not due:
        return

    async def _send_reminders() -> None:
        bot = Bot(token=settings.telegram_bot_token)
        try:
            for reminder in due:
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
