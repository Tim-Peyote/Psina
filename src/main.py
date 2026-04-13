import asyncio
import structlog

from aiogram.types import Message

# MUST be first: register LLM providers before anything else uses them
from src.llm_adapter import registry  # noqa: F401

from src.config import settings
from src.telegram_gateway.bot import BotManager
from src.telegram_gateway.router import GatewayRouter
from src.database.session import async_session_factory

structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
)

logger = structlog.get_logger()


async def middleware_update_user_chat(handler, event, data):
    """Middleware to ensure user and chat exist in DB before handling."""
    from aiogram.types import Message
    if isinstance(event, Message) and event.from_user:
        from src.database.models import User as UserModel, Chat as ChatModel, ChatType
        from sqlalchemy.dialects.postgresql import insert

        async with async_session_factory() as session:
            try:
                # Upsert user (race-condition safe via ON CONFLICT DO UPDATE)
                stmt_user = insert(UserModel).values(
                    id=event.from_user.id,
                    username=event.from_user.username,
                    first_name=event.from_user.first_name,
                    last_name=event.from_user.last_name,
                    language_code=event.from_user.language_code,
                    is_bot=event.from_user.is_bot or False,
                )
                stmt_user = stmt_user.on_conflict_do_update(
                    index_elements=[UserModel.id],
                    set_=dict(
                        username=event.from_user.username,
                        first_name=event.from_user.first_name,
                        last_name=event.from_user.last_name,
                        language_code=event.from_user.language_code,
                    ),
                )
                await session.execute(stmt_user)

                # Upsert chat (race-condition safe via ON CONFLICT DO UPDATE)
                chat_type = ChatType(event.chat.type)
                stmt_chat = insert(ChatModel).values(
                    id=event.chat.id,
                    type=chat_type,
                    title=event.chat.title,
                )
                stmt_chat = stmt_chat.on_conflict_do_update(
                    index_elements=[ChatModel.id],
                    set_=dict(
                        title=event.chat.title,
                        type=chat_type,
                    ),
                )
                await session.execute(stmt_chat)

                await session.commit()
            except Exception:
                logger.exception("Failed to upsert user/chat")
    return await handler(event, data)


async def main() -> None:
    logger.info("Starting Zalutka bot")

    # Discover skills from SKILL.md files (lazy: only name + description)
    from src.skill_system.registry import skill_registry
    await skill_registry.discover_skills()

    # Load persisted vibe profiles so they survive restarts
    from src.orchestration_engine.vibe_adapter import vibe_adapter
    await vibe_adapter.load_all_profiles()

    bot_manager = BotManager()
    gateway = GatewayRouter()

    dp = bot_manager.get_dispatcher()
    dp.include_router(gateway.get_router())

    # Register middleware
    dp.message.middleware(middleware_update_user_chat)

    logger.info("Bot initialized")

    # Run polling and proactive loop concurrently
    proactive_task = asyncio.create_task(_proactive_loop())
    try:
        await asyncio.gather(
            bot_manager.start_polling(),
            proactive_task,
        )
    except Exception:
        logger.exception("Main loop crashed")
    finally:
        proactive_task.cancel()
        try:
            await proactive_task
        except asyncio.CancelledError:
            pass
        await bot_manager.shutdown()


async def _proactive_loop(interval: int = 300) -> None:
    """Background task: check chats for proactive messages every `interval` seconds.

    Runs in the main bot process — no Celery fork, no event loop mismatch.
    """
    import random
    from datetime import datetime, timezone, timedelta
    from aiogram import Bot
    from src.config import settings
    from src.database.session import async_session_factory
    from src.database.models import Chat, ChatType
    from src.telegram_gateway.message_postprocessor import message_postprocessor
    from sqlalchemy import select
    import redis.asyncio as aioredis

    logger.info("Proactive loop started", interval=interval)

    bot = Bot(token=settings.telegram_bot_token)
    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    PREFIX = "proactive:"
    _TZ_MSK = timezone(timedelta(hours=3))

    try:
        while True:
            try:
                now_msk = datetime.now(_TZ_MSK)
                hour = now_msk.hour

                # Quiet hours
                if not (settings.quiet_hours_start <= hour or hour < settings.quiet_hours_end):
                    # Morning greeting window
                    if 8 <= hour <= 10:
                        async with async_session_factory() as session:
                            stmt = select(Chat).where(
                                Chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP])
                            )
                            result = await session.execute(stmt)
                            chats = list(result.scalars().all())

                        for chat in chats:
                            try:
                                type_key = f"{PREFIX}type:{chat.id}:morning"
                                if not await r.get(type_key):
                                    count_key = f"{PREFIX}count:{chat.id}"
                                    count = await r.get(count_key)
                                    if count and int(count) >= settings.proactive_max_per_hour:
                                        continue

                                    greetings = [
                                        "Доброе утро! Как спалось?",
                                        "Утро! Новый день, новые приключения",
                                        "Доброе утречко! Я бодрый и готов к делу",
                                    ]
                                    msg = random.choice(greetings)
                                    formatted = message_postprocessor.process(msg)
                                    await bot.send_message(chat_id=chat.id, text=formatted, parse_mode="HTML")
                                    now_utc = datetime.now(timezone.utc)
                                    await r.set(type_key, "1", ex=86400)
                                    await r.set(f"{PREFIX}last:{chat.id}", now_utc.isoformat(), ex=settings.proactive_cooldown_seconds)
                                    pipe = r.pipeline()
                                    pipe.incr(count_key)
                                    pipe.expire(count_key, 3600)
                                    await pipe.execute()
                                    logger.info("Proactive morning sent", chat_id=chat.id)
                                    break  # One greeting per cycle
                            except Exception:
                                logger.debug("Proactive chat error", chat_id=chat.id, exc_info=True)
            except Exception:
                logger.debug("Proactive loop cycle error", exc_info=True)

            await asyncio.sleep(interval)
    finally:
        await bot.session.close()
        await r.aclose()
        logger.info("Proactive loop stopped")


if __name__ == "__main__":
    asyncio.run(main())
