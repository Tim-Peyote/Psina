"""
Fact extractor — автоматически извлекает факты о людях из сообщений.

Примеры:
- «я люблю пиццу» → факт: user_id любит пиццу
- «Маша работает в Google» → факт: Маша работает в Google
- «мы с Петей ходили в кино» → факт: связь между user_id и Петей
"""

import re
import json

import structlog

from src.llm_adapter.base import LLMProvider
from src.database.session import get_session
from src.database.models import MemoryItem, MemoryType, UserProfile
from src.message_processor.processor import NormalizedMessage
from sqlalchemy import select

logger = structlog.get_logger()

# Паттерны для быстрого извлечения без LLM
DIRECT_FACT_PATTERNS = [
    # «я люблю/обожаю/предпочитаю X»
    (r'(?:я люблю|я обожаю|я предпочитаю|мне нравится|мне по душе)\s+(.+)', "preference"),
    # «я работаю в/на X»
    (r'(?:я работаю в|я работаю на|работаю в|работаю на)\s+(.+)', "fact"),
    # «мне X лет»
    (r'(?:мне)\s+(\d+)\s+(?:лет|года|год)', "fact"),
    # «я живу в X»
    (r'(?:я живу в|я живу на|живу в|живу на)\s+(.+)', "fact"),
    # «у меня есть X»
    (r'(?:у меня есть|у меня имеется)\s+(.+)', "fact"),
    # «я из X»
    (r'(?:я из|родом из)\s+(.+)', "fact"),
    # «мой любимый X — Y»
    (r'(?:мой|моя|моё)\s+любим[ый|ая|ое|ые]\s+(\w+)\s+(?:это|—|-|:)\s*(.+)', "preference"),
]

# Паттерны о других людях
OTHER_FACT_PATTERNS = [
    # «X работает в Y»
    (r'([А-ЯA-Z][а-яa-z]+)\s+(?:работает в|работает на)\s+(.+)', "fact"),
    # «X живёт в Y»
    (r'([А-ЯA-Z][а-яa-z]+)\s+(?:живёт в|живёт на|живет в|живет на)\s+(.+)', "fact"),
    # «X любит Y»
    (r'([А-ЯA-Z][а-яa-z]+)\s+(?:любит|обожает|предпочитает)\s+(.+)', "preference"),
]


class FactExtractor:
    """Извлекает факты из сообщений и сохраняет в память."""

    def __init__(self) -> None:
        self._llm = LLMProvider.get_provider()

    async def extract_and_save(self, msg: NormalizedMessage) -> list[str]:
        """
        Извлечь факты из сообщения и сохранить.
        Возвращает список извлечённых фактов.
        """
        facts: list[str] = []

        # 1. Быстрое извлечение по паттернам
        pattern_facts = self._extract_by_patterns(msg)
        facts.extend(pattern_facts)

        # 2. LLM извлечение для сложных случаев
        if len(msg.text) > 20:  # Только для осмысленных сообщений
            llm_facts = await self._extract_by_llm(msg)
            facts.extend(llm_facts)

        # 3. Сохраняем факты
        if facts:
            await self._save_facts(msg, facts)

        return facts

    def _extract_by_patterns(self, msg: NormalizedMessage) -> list[str]:
        """Извлечь факты по regex паттернам."""
        facts = []
        text = msg.text.strip()

        # Факты о себе
        for pattern, fact_type in DIRECT_FACT_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                content = match.group(0).strip()
                facts.append(f"[О себе] {content}")

        # Факты о других
        for pattern, fact_type in OTHER_FACT_PATTERNS:
            match = re.search(pattern, text)
            if match:
                person = match.group(1)
                content = match.group(0).strip()
                facts.append(f"[О {person}] {content}")

        return facts

    async def _extract_by_llm(self, msg: NormalizedMessage) -> list[str]:
        """Извлечь факты через LLM."""
        try:
            facts = await self._llm.extract_facts(msg.text)
            return facts
        except Exception:
            logger.exception("LLM fact extraction failed")
            return []

    async def _save_facts(self, msg: NormalizedMessage, facts: list[str]) -> None:
        """Сохранить извлечённые факты в БД."""
        async for session in get_session():
            for fact_text in facts:
                # Определяем тип факта
                fact_type = MemoryType.FACT
                if "[О себе]" in fact_text or "preference" in fact_text.lower():
                    fact_type = MemoryType.PREFERENCE
                elif "работает" in fact_text.lower() or "жив" in fact_text.lower():
                    fact_type = MemoryType.FACT

                # Проверяем дубликаты
                existing = await self._find_similar_fact(session, msg.user_id, fact_text)
                if existing:
                    # Обновляем существующий факт — повышаем confidence
                    existing.confidence = min(existing.confidence + 0.1, 1.0)
                    continue

                session.add(
                    MemoryItem(
                        chat_id=msg.chat_id,
                        user_id=msg.user_id,
                        type=fact_type,
                        content=fact_text,
                        confidence=0.7,
                        relevance=0.8,
                        source="fact_extractor",
                    )
                )

            await session.commit()

    async def _find_similar_fact(
        self, session, user_id: int, content: str
    ) -> MemoryItem | None:
        """Найти похожий факт в базе (простая проверка по подстроке)."""
        # Берём последние факты пользователя
        stmt = (
            select(MemoryItem)
            .where(
                MemoryItem.user_id == user_id,
                MemoryItem.type.in_([MemoryType.FACT, MemoryType.PREFERENCE]),
            )
            .order_by(MemoryItem.created_at.desc())
            .limit(50)
        )
        result = await session.execute(stmt)
        items = list(result.scalars().all())

        # Проверяем пересечение слов
        words = set(content.lower().split())
        for item in items:
            item_words = set(item.content.lower().split())
            overlap = len(words & item_words)
            if overlap >= 3 or content.lower() in item.content.lower() or item.content.lower() in content.lower():
                return item

        return None

    async def update_profile_from_facts(self, user_id: int) -> None:
        """Обновить профиль пользователя на основе накопленных фактов."""
        async for session in get_session():
            # Собираем все факты о пользователе
            stmt = (
                select(MemoryItem)
                .where(
                    MemoryItem.user_id == user_id,
                    MemoryItem.type.in_([MemoryType.FACT, MemoryType.PREFERENCE]),
                )
                .order_by(MemoryItem.created_at.desc())
                .limit(30)
            )
            result = await session.execute(stmt)
            facts = list(result.scalars().all())

            if not facts:
                return

            # Получаем или создаём профиль
            profile_stmt = select(UserProfile).where(UserProfile.user_id == user_id)
            profile_result = await session.execute(profile_stmt)
            profile = profile_result.scalar_one_or_none()

            if not profile:
                profile = UserProfile(user_id=user_id)
                session.add(profile)

            # Группируем факты
            interests = []
            traits = []
            for fact in facts:
                text = fact.content.lower()
                if any(w in text for w in ["любит", "обожает", "нравится", "предпочитает", "любим"]):
                    interests.append(fact.content)
                elif any(w in text for w in ["работает", "жив", "учится", "из "]):
                    traits.append(fact.content)

            if interests:
                profile.interests = json.dumps(list(set(interests)))
            if traits:
                profile.traits = json.dumps(list(set(traits)))

            # Создаём краткое резюме
            if facts:
                summary_parts = []
                if interests:
                    summary_parts.append(f"Интересы: {', '.join(interests[:3])}")
                if traits:
                    summary_parts.append(f"Инфо: {', '.join(traits[:3])}")
                profile.summary = ". ".join(summary_parts)

            await session.commit()


fact_extractor = FactExtractor()
