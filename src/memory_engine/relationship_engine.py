"""
Relationship engine — отслеживает связи между пользователями.

Понимает:
- Кто с кем общается
- Общие темы
- Историю взаимодействий
- Степень близости (по частоте общения)
"""

import json
from datetime import datetime, timezone

import structlog
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.session import get_session
from src.database.models import MemoryItem, MemoryType, UserProfile
from src.message_processor.processor import NormalizedMessage

logger = structlog.get_logger()


class RelationshipEngine:
    """
    Отслеживает и обновляет связи между пользователями.
    """

    async def track_interaction(
        self,
        from_user_id: int,
        to_user_id: int,
        context: str,
    ) -> None:
        """
        Зафиксировать взаимодействие между пользователями.
        """
        if from_user_id == to_user_id:
            return

        async for session in get_session():
            # Ищем существующую запись о связи
            rel_content = f"взаимодействие: {from_user_id} ↔ {to_user_id}"
            stmt = select(MemoryItem).where(
                and_(
                    MemoryItem.type == MemoryType.RELATIONSHIP,
                    MemoryItem.user_id == from_user_id,
                    MemoryItem.content.ilike(f"%{to_user_id}%"),
                )
            )
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()

            if existing:
                # Обновляем существующую связь
                existing.confidence = min(existing.confidence + 0.05, 1.0)
                existing.relevance = min(existing.relevance + 0.1, 1.0)
                existing.content = f"{existing.content} | {context}"
            else:
                session.add(
                    MemoryItem(
                        user_id=from_user_id,
                        type=MemoryType.RELATIONSHIP,
                        content=f"общается с user_{to_user_id}: {context}",
                        confidence=0.5,
                        relevance=0.6,
                        source="relationship_tracker",
                    )
                )
                # Обратная связь
                session.add(
                    MemoryItem(
                        user_id=to_user_id,
                        type=MemoryType.RELATIONSHIP,
                        content=f"общается с user_{from_user_id}: {context}",
                        confidence=0.5,
                        relevance=0.6,
                        source="relationship_tracker",
                    )
                )

            await session.commit()

    async def extract_relationship_from_message(self, msg: NormalizedMessage) -> None:
        """
        Извлечь связи из сообщения.
        Например: «мы с Петей...» → связь между автором и Петей.
        """
        text = msg.text.lower()

        # Паттерны связей
        relationship_patterns = [
            (r'мы с\s+([А-ЯA-Z][а-яa-z]+)', "вместе с"),
            (r'мой друг\s+([А-ЯA-Z][а-яa-z]+)', "друг"),
            (r'моя подруга\s+([А-ЯA-Z][а-яa-z]+)', "друг"),
            (r'мой парень\s+([А-ЯA-Z][а-яa-z]+)', "партнёр"),
            (r'моя девушка\s+([А-ЯA-Z][а-яa-z]+)', "партнёр"),
            (r'мой брат\s+([А-ЯA-Z][а-яa-z]+)', "брат"),
            (r'моя сестр[а|ы]\s+([А-ЯA-Z][а-яa-z]+)', "сестра"),
            (r'мой коллега\s+([А-ЯA-Z][а-яa-z]+)', "коллега"),
            (r'мой начальник\s+([А-ЯA-Z][а-яa-z]+)', "начальник"),
        ]

        import re
        for pattern, rel_type in relationship_patterns:
            match = re.search(pattern, text)
            if match:
                person_name = match.group(1)
                # Пытаемся найти user_id по имени
                from src.context_tracker.tracker import context_tracker
                other_id = context_tracker.resolve_name(person_name)

                if other_id:
                    await self.track_interaction(
                        msg.user_id,
                        other_id,
                        f"{rel_type}: {person_name}",
                    )
                    logger.info(
                        "Relationship detected",
                        from_user=msg.user_id,
                        to_user=other_id,
                        rel_type=rel_type,
                        name=person_name,
                    )

    async def get_user_relationships(self, user_id: int, chat_id: int) -> list[dict]:
        """Получить все связи пользователя в конкретном чате."""
        async for session in get_session():
            stmt = (
                select(MemoryItem)
                .where(
                    MemoryItem.user_id == user_id,
                    MemoryItem.chat_id == chat_id,
                    MemoryItem.type == MemoryType.RELATIONSHIP,
                )
                .order_by(MemoryItem.relevance.desc())
                .limit(20)
            )
            result = await session.execute(stmt)
            items = list(result.scalars().all())

            relationships = []
            for item in items:
                relationships.append({
                    "content": item.content,
                    "confidence": item.confidence,
                    "created_at": item.created_at.isoformat(),
                })

            return relationships

    async def update_profile_relationships(self, user_id: int, chat_id: int) -> None:
        """Обновить раздел отношений в профиле пользователя для конкретного чата."""
        relationships = await self.get_user_relationships(user_id, chat_id)

        if not relationships:
            return

        async for session in get_session():
            stmt = select(UserProfile).where(
                UserProfile.user_id == user_id,
                UserProfile.chat_id == chat_id,
            )
            result = await session.execute(stmt)
            profile = result.scalar_one_or_none()

            if not profile:
                return

            # Формируем строку отношений
            rel_texts = [r["content"] for r in relationships[:10]]
            profile.relationships = json.dumps(rel_texts)

            await session.commit()


relationship_engine = RelationshipEngine()
