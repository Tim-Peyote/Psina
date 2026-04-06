from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.database.session import get_session
from src.database.models import (
    MemoryItem, MemoryType, UserProfile, Chat, ChatType,
    UsageStat, Message, MessageRole,
)
from src.message_processor.processor import NormalizedMessage
from src.memory_engine.importance import calculate_importance
from src.memory_engine.dedup import content_hash, is_duplicate

logger = structlog.get_logger()


class MemoryEngine:
    """Центр управления памятью бота."""

    def __init__(self) -> None:
        self._chat_modes: dict[int, str] = {}
        self._recent_hashes: dict[int, set[str]] = {}  # chat_id -> set of hashes

    async def ingest_message(self, msg: NormalizedMessage) -> None:
        """Сохранить сообщение в память + извлечь факты."""
        # Проверяем дубликат
        chat_hashes = self._recent_hashes.get(msg.chat_id, set())
        msg_hash = content_hash(msg.text)
        if is_duplicate(chat_hashes, msg.text):
            logger.debug("Duplicate message skipped", text=msg.text[:50])
            return

        chat_hashes.add(msg_hash)
        self._recent_hashes[msg.chat_id] = chat_hashes

        # Ограничиваем хэши
        if len(chat_hashes) > 500:
            self._recent_hashes[msg.chat_id] = set(list(chat_hashes)[-200:])

        importance = calculate_importance(msg)

        async for session in get_session():
            memory_item = MemoryItem(
                chat_id=msg.chat_id,
                user_id=msg.user_id,
                type=MemoryType.RAW_MESSAGE,
                content=msg.text,
                confidence=0.8,
                relevance=importance,
                source="telegram",
            )
            session.add(memory_item)
            await session.commit()

    async def save_bot_response(self, text: str, chat_id: int, user_id: int) -> None:
        """Сохранить ответ бота."""
        async for session in get_session():
            session.add(
                MemoryItem(
                    chat_id=chat_id,
                    user_id=user_id,
                    type=MemoryType.FACT,
                    content=text,
                    confidence=1.0,
                    relevance=0.5,
                    source="bot_response",
                )
            )
            await session.commit()

    async def get_recent_memories(
        self, user_id: int, chat_id: int, limit: int = 10
    ) -> list[MemoryItem]:
        """Получить недавние воспоминания (кроме сырых сообщений)."""
        async for session in get_session():
            stmt = (
                select(MemoryItem)
                .where(
                    and_(
                        MemoryItem.chat_id == chat_id,
                        MemoryItem.type != MemoryType.RAW_MESSAGE,
                    )
                )
                .order_by(MemoryItem.relevance.desc(), MemoryItem.created_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_user_profile(self, user_id: int) -> UserProfile | None:
        """Получить профиль пользователя."""
        async for session in get_session():
            stmt = select(UserProfile).where(UserProfile.user_id == user_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def get_chat_mode(self, chat_id: int) -> "BotMode":
        """Получить режим бота для чата."""
        from src.orchestration_engine.orchestrator import BotMode

        if chat_id in self._chat_modes:
            return BotMode(self._chat_modes[chat_id])

        async for session in get_session():
            stmt = select(Chat).where(Chat.id == chat_id)
            result = await session.execute(stmt)
            chat = result.scalar_one_or_none()
            if chat:
                mode = BotMode(chat.bot_mode)
                self._chat_modes[chat_id] = chat.bot_mode
                return mode

        return BotMode(settings.bot_mode)

    async def set_chat_mode(self, chat_id: int, mode: str) -> None:
        """Установить режим бота."""
        self._chat_modes[chat_id] = mode

        async for session in get_session():
            from sqlalchemy import select
            stmt = select(Chat).where(Chat.id == chat_id)
            result = await session.execute(stmt)
            chat = result.scalar_one_or_none()
            if chat:
                chat.bot_mode = mode
                await session.commit()
            else:
                session.add(
                    Chat(
                        id=chat_id,
                        type=ChatType.PRIVATE,
                        bot_mode=mode,
                    )
                )
                await session.commit()

    async def get_today_usage(self) -> UsageStat:
        """Получить использование токенов за сегодня."""
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

        async for session in get_session():
            stmt = (
                select(UsageStat)
                .where(UsageStat.date == today)
                .order_by(UsageStat.created_at.desc())
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
