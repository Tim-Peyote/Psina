import structlog
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.database.session import get_session
from src.database.models import MemoryItem, MemoryType, Message, UserProfile
from src.message_processor.processor import NormalizedMessage
from src.context_tracker.tracker import context_tracker

logger = structlog.get_logger()


class Retriever:
    """
    Гибридный поиск контекста.
    Собирает всё что нужно LLM для осмысленного ответа.
    """

    async def retrieve(self, msg: NormalizedMessage) -> list[dict]:
        """Собрать весь контекст для сообщения."""
        messages: list[dict] = []

        # 1. Контекст из context tracker (кто, кому, о ком)
        context = context_tracker.get_context_for_message(msg)

        # 2. Недавние сообщения чата
        recent = await self._get_recent_messages(msg.chat_id, limit=20)
        for m in recent:
            author = m.user_id  # later resolved to name
            messages.append({"role": "user", "content": m.text})

        # 3. Профиль пользователя
        profile = await self._get_user_profile(msg.user_id, msg.chat_id)
        if profile:
            profile_ctx = f"Профиль пользователя: {profile.summary or 'нет данных'}"
            if profile.interests:
                import json
                interests = json.loads(profile.interests)
                profile_ctx += f"\nИнтересы: {', '.join(interests[:5])}"
            if profile.traits:
                import json
                traits = json.loads(profile.traits)
                profile_ctx += f"\nИнфо: {', '.join(traits[:5])}"
            if profile.relationships:
                import json
                rels = json.loads(profile.relationships)
                profile_ctx += f"\nСвязи: {', '.join(rels[:5])}"
            messages.append({"role": "system", "content": profile_ctx})

        # 4. Релевантная память
        memories = await self._get_relevant_memories(msg.user_id, msg.chat_id)
        if memories:
            memory_context = "Контекст памяти:\n" + "\n".join(f"- {m.content}" for m in memories)
            messages.append({"role": "system", "content": memory_context})

        # 5. Игровой контекст
        from src.game_engine.manager import GameManager
        game_manager = GameManager()
        game_ctx = await game_manager.get_active_session(msg.chat_id)
        if game_ctx:
            messages.append({"role": "system", "content": f"Активная игра: {game_ctx.name}"})

        # Лимит токенов
        messages = self._enforce_token_limit(messages)

        return messages

    async def _get_recent_messages(self, chat_id: int, limit: int = 20) -> list[Message]:
        async for session in get_session():
            stmt = (
                select(Message)
                .where(Message.chat_id == chat_id)
                .order_by(Message.created_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            return list(reversed(result.scalars().all()))

    async def _get_user_profile(self, user_id: int, chat_id: int) -> UserProfile | None:
        async for session in get_session():
            stmt = select(UserProfile).where(
                UserProfile.user_id == user_id,
                UserProfile.chat_id == chat_id,
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def _get_relevant_memories(
        self, user_id: int, chat_id: int, limit: int = 10
    ) -> list[MemoryItem]:
        async for session in get_session():
            stmt = (
                select(MemoryItem)
                .where(
                    and_(
                        MemoryItem.chat_id == chat_id,
                        MemoryItem.type.in_([
                            MemoryType.FACT,
                            MemoryType.PREFERENCE,
                            MemoryType.EVENT,
                            MemoryType.GROUP_RULE,
                            MemoryType.RELATIONSHIP,
                        ]),
                    )
                )
                .order_by(MemoryItem.relevance.desc(), MemoryItem.created_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    def _enforce_token_limit(self, messages: list[dict]) -> list[dict]:
        """Обрезать сообщения сверх лимита."""
        max_chars = settings.max_context_tokens * 4
        total_chars = 0
        result = []

        # Системные сообщения всегда в начале
        system_msgs = [m for m in messages if m["role"] == "system"]
        user_msgs = [m for m in messages if m["role"] == "user"]

        for msg in system_msgs:
            content_len = len(msg.get("content", ""))
            if total_chars + content_len <= max_chars:
                result.append(msg)
                total_chars += content_len

        # Последние сообщения важнее
        for msg in reversed(user_msgs):
            content_len = len(msg.get("content", ""))
            if total_chars + content_len <= max_chars:
                result.append(msg)
                total_chars += content_len
            else:
                break

        return list(reversed(result))
