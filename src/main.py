import asyncio
import structlog

from aiogram.types import Message

from src.config import settings
from src.telegram_gateway.bot import BotManager
from src.telegram_gateway.router import GatewayRouter
from src.database.session import async_session_factory
from src.llm_adapter import registry  # noqa: F401 — register providers

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
        gateway = GatewayRouter()
        async with async_session_factory() as session:
            try:
                await gateway.ensure_user(session, event.from_user)
                await gateway.ensure_chat(session, event.chat)
            except Exception:
                logger.exception("Failed to upsert user/chat")
    return await handler(event, data)


async def main() -> None:
    logger.info("Starting Zalutka bot")

    # Discover skills from SKILL.md files (lazy: only name + description)
    from src.skill_system.registry import skill_registry
    await skill_registry.discover_skills()

    bot_manager = BotManager()
    gateway = GatewayRouter()

    dp = bot_manager.get_dispatcher()
    dp.include_router(gateway.get_router())

    # Register middleware
    dp.message.middleware(middleware_update_user_chat)

    logger.info("Bot initialized")
    await bot_manager.start_polling()


if __name__ == "__main__":
    asyncio.run(main())
