import structlog

from aiogram import Router as AiogramRouter
from aiogram.types import User, Chat
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import User as UserModel, Chat as ChatModel, ChatType
from src.telegram_gateway.handlers import router as handlers_router

logger = structlog.get_logger()


class GatewayRouter:
    """Central router that wires up handlers and tracks users/chats."""

    def __init__(self) -> None:
        self.router = AiogramRouter()
        self.router.include_router(handlers_router)

    def get_router(self) -> AiogramRouter:
        return self.router

    async def ensure_user(self, session: AsyncSession, telegram_user: User) -> None:
        """Upsert user in the database."""
        result = await session.execute(select(UserModel).where(UserModel.id == telegram_user.id))
        existing = result.scalar_one_or_none()

        if existing:
            existing.username = telegram_user.username
            existing.first_name = telegram_user.first_name
            existing.last_name = telegram_user.last_name
            existing.language_code = telegram_user.language_code
        else:
            session.add(
                UserModel(
                    id=telegram_user.id,
                    username=telegram_user.username,
                    first_name=telegram_user.first_name,
                    last_name=telegram_user.last_name,
                    language_code=telegram_user.language_code,
                    is_bot=telegram_user.is_bot or False,
                )
            )
        await session.commit()

    async def ensure_chat(self, session: AsyncSession, telegram_chat: Chat) -> None:
        """Upsert chat in the database."""
        result = await session.execute(select(ChatModel).where(ChatModel.id == telegram_chat.id))
        existing = result.scalar_one_or_none()

        chat_type = ChatType(telegram_chat.type)
        if existing:
            existing.title = telegram_chat.title
            existing.type = chat_type
        else:
            session.add(
                ChatModel(
                    id=telegram_chat.id,
                    type=chat_type,
                    title=telegram_chat.title,
                )
            )
        await session.commit()
