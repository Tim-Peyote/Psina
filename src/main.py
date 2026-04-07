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
        from src.database.models import User as UserModel, Chat as ChatModel, ChatType
        from sqlalchemy import select

        async with async_session_factory() as session:
            try:
                # Ensure user
                result = await session.execute(select(UserModel).where(UserModel.id == event.from_user.id))
                existing_user = result.scalar_one_or_none()
                if existing_user:
                    existing_user.username = event.from_user.username
                    existing_user.first_name = event.from_user.first_name
                else:
                    session.add(
                        UserModel(
                            id=event.from_user.id,
                            username=event.from_user.username,
                            first_name=event.from_user.first_name,
                            last_name=event.from_user.last_name,
                            language_code=event.from_user.language_code,
                            is_bot=event.from_user.is_bot or False,
                        )
                    )
                # Ensure chat
                result = await session.execute(select(ChatModel).where(ChatModel.id == event.chat.id))
                existing_chat = result.scalar_one_or_none()
                chat_type = ChatType(event.chat.type)
                if existing_chat:
                    existing_chat.title = event.chat.title
                    existing_chat.type = chat_type
                else:
                    session.add(
                        ChatModel(
                            id=event.chat.id,
                            type=chat_type,
                            title=event.chat.title,
                        )
                    )
                await session.commit()
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
