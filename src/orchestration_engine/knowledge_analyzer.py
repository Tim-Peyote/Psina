"""
Knowledge Analyzer — анализ накопленных знаний перед ответом.

Перед генерацией ответа Псина:
1. Смотрит что она уже знает по теме
2. Оценивает уверенность
3. Решает — ответить с фактами / уточнить / признаться что не знает
"""

import re
from dataclasses import dataclass, field
from enum import Enum

import structlog

from src.message_processor.processor import NormalizedMessage
from src.memory_engine.engine import MemoryEngine
from src.database.models import MemoryItem, MemoryType, UserProfile

logger = structlog.get_logger()


class ConfidenceLevel(Enum):
    CERTAIN = "certain"      # точно помню
    UNCERTAIN = "uncertain"  # смутно помню
    UNKNOWN = "unknown"      # не знаю


@dataclass
class KnowledgeItem:
    content: str
    confidence: float
    source: str  # откуда: пользователь, группа, дата
    level: ConfidenceLevel


@dataclass
class KnowledgeReport:
    """Результат анализа знаний по запросу."""

    query: str
    target_person: str | None = None
    topic: str | None = None
    facts: list[KnowledgeItem] = field(default_factory=list)
    preferences: list[KnowledgeItem] = field(default_factory=list)
    relationships: list[KnowledgeItem] = field(default_factory=list)
    has_enough_info: bool = False
    clarification_needed: str | None = None  # если нужно уточнить

    @property
    def summary_text(self) -> str:
        """Текстовая сводка знаний для LLM."""
        if not self.facts and not self.preferences and not self.relationships:
            return "Нет релевантных знаний по этой теме."

        parts = []

        if self.facts:
            certain = [f for f in self.facts if f.level == ConfidenceLevel.CERTAIN]
            uncertain = [f for f in self.facts if f.level == ConfidenceLevel.UNCERTAIN]

            if certain:
                lines = [f.content for f in certain[:3]]
                parts.append(f"Уверенные факты:\n" + "\n".join(f"- {l}" for l in lines))
            if uncertain:
                lines = [f.content for f in uncertain[:3]]
                parts.append(f"Смутно помню:\n" + "\n".join(f"- {l}" for l in lines))

        if self.preferences:
            lines = [f.content for f in self.preferences[:3]]
            parts.append(f"Предпочтения:\n" + "\n".join(f"- {l}" for l in lines))

        if self.relationships:
            lines = [f.content for f in self.relationships[:3]]
            parts.append(f"Связи:\n" + "\n".join(f"- {l}" for l in lines))

        return "\n\n".join(parts)

    @property
    def clarification_prompt(self) -> str | None:
        """Если нужно уточнить — что спросить."""
        if self.clarification_needed and not self.has_enough_info:
            return f"У меня мало данных. {self.clarification_needed}"
        return None


# Паттерны для определения что спрашивают
QUESTION_TARGET_PATTERNS = [
    # «что X любит»
    (r'(?:что|чем)\s+(.+?)\s+(?:любит|обожает|предпочитает|увлекается|интересуется)', "preference"),
    # «какой любимый X у Y»
    (r'какой\s+(\w+)\s+(?:у|любит)\s+(.+)', "preference"),
    # «кто такой X»
    (r'(?:кто\s+такой|кто\s+такая|расскажи\s+про|что\s+знаешь\s+про|что\s+знаешь\s+о)\s+(.+)', "person"),
    # «где X работает»
    (r'(?:где|кем)\s+(.+?)\s+(?:работает|учится|живёт)', "fact"),
    # «когда X ...»
    (r'(?:когда|во\s+сколько|в\s+какое\s+время)\s+(.+)', "time"),
]

# Паттерны для извлечения имени из запроса
NAME_FROM_QUESTION = re.compile(
    r'(?:у|про|о|кем|где|кто|какой|какая|какое|что)\s+([А-ЯA-Z][а-яa-z]+)',
    re.IGNORECASE
)


class KnowledgeAnalyzer:
    """
    Анализирует накопленные знания перед ответом.

    КРИТИЧЕСКИ ВАЖНО: знания из чата А НИКОГДА не попадают в чат Б.
    """

    # Строгий запрет на межкаутную утечку
    CROSS_CHAT_LEAK_PROTECTION = True

    def __init__(self) -> None:
        self.memory_engine = MemoryEngine()

    async def analyze(
        self,
        msg: NormalizedMessage,
        context: dict | None = None,
    ) -> KnowledgeReport:
        """
        Проанализировать знания по запросу.
        Возвращает KnowledgeReport.
        """
        text = msg.text.lower()

        report = KnowledgeReport(query=msg.text)

        # 1. Определяем о ком/чём спрашивают
        target = self._extract_target(msg.text)
        if target:
            report.target_person = target
            logger.debug("Target extracted", target=target)

        # 2. Определяем тему вопроса
        topic = self._extract_topic(text)
        if topic:
            report.topic = topic

        # 3. Собираем релевантную память из ЭТОГО чата
        relevant_memories = await self._get_relevant_memories(msg.chat_id, target)

        # 4. Классифицируем знания
        for memory in relevant_memories:
            item = KnowledgeItem(
                content=memory.content,
                confidence=memory.confidence,
                source=memory.source,
                level=self._confidence_to_level(memory.confidence),
            )

            if memory.type == MemoryType.PREFERENCE:
                report.preferences.append(item)
            elif memory.type == MemoryType.RELATIONSHIP:
                report.relationships.append(item)
            elif memory.type in (MemoryType.FACT, MemoryType.EVENT):
                report.facts.append(item)

        # 5. Также берём глобальный профиль пользователя
        if msg.user_id and not report.facts:
            profile = await self.memory_engine.get_user_profile(msg.user_id)
            if profile and profile.summary:
                report.facts.append(KnowledgeItem(
                    content=f"Профиль пользователя: {profile.summary}",
                    confidence=0.6,
                    source="user_profile",
                    level=ConfidenceLevel.UNCERTAIN,
                ))

        # 6. Решаем — достаточно ли информации
        total_items = len(report.facts) + len(report.preferences) + len(report.relationships)
        certain_count = sum(
            1 for items in [report.facts, report.preferences, report.relationships]
            for item in items if item.level == ConfidenceLevel.CERTAIN
        )

        if certain_count >= 1 or total_items >= 2:
            report.has_enough_info = True
        else:
            report.has_enough_info = False
            report.clarification_needed = self._generate_clarification(topic, target)

        logger.debug(
            "Knowledge analyzed",
            target=report.target_person,
            topic=report.topic,
            facts=len(report.facts),
            has_enough=report.has_enough_info,
        )

        return report

    def should_respond_with_facts(self, report: KnowledgeReport) -> bool:
        """Можно ли отвечать с фактами из памяти."""
        return report.has_enough_info

    def should_ask_for_clarification(self, report: KnowledgeReport) -> bool:
        """Нужно ли уточнить вместо ответа."""
        return not report.has_enough_info and report.clarification_needed is not None

    def get_response_strategy(self, report: KnowledgeReport) -> str:
        """
        Определить страте ответа:
        - "answer_with_facts" — есть данные
        - "answer_with_uncertainty" — данные есть но не уверен
        - "ask_clarification" — мало данных, уточнить
        - "admit_ignorance" — вообще ничего не знаю
        """
        if not report.facts and not report.preferences and not report.relationships:
            return "admit_ignorance"

        if report.has_enough_info:
            certain = any(f.level == ConfidenceLevel.CERTAIN for f in report.facts)
            if certain:
                return "answer_with_facts"
            return "answer_with_uncertainty"

        return "ask_clarification"

    # ========== Внутренние методы ==========

    def _extract_target(self, text: str) -> str | None:
        """Извлечь о ком спрашивают."""
        # Ищем имена в вопросе
        matches = NAME_FROM_QUESTION.findall(text)
        if matches:
            # Берём первое найденное имя (не бота)
            from src.config import settings
            bot_names = {settings.bot_name.lower()} | {a.lower() for a in settings.bot_aliases}
            for name in matches:
                if name.lower() not in bot_names:
                    return name

        # Извлекаем все имена
        from src.context_tracker.tracker import context_tracker
        all_names = re.findall(r'([А-ЯA-Z][а-яa-z]{2,15})', text)
        bot_names = {settings.bot_name.lower()} | {a.lower() for a in settings.bot_aliases}
        for name in all_names:
            if name.lower() not in bot_names:
                resolved = context_tracker.resolve_name(name)
                if resolved:
                    return name
                return name

        return None

    def _extract_topic(self, text: str) -> str | None:
        """Определить тему вопроса."""
        text_lower = text.lower()

        for pattern, topic_type in QUESTION_TARGET_PATTERNS:
            if re.search(pattern, text_lower):
                return topic_type

        # Ключевые слова
        topic_words = {
            "любит": "preference",
            "нравится": "preference",
            "работа": "fact",
            "жив": "fact",
            "где": "fact",
            "когда": "time",
            "сколько": "time",
            "друг": "relationship",
            "знаком": "relationship",
        }

        for word, topic in topic_words.items():
            if word in text_lower:
                return topic

        return None

    async def _get_relevant_memories(
        self,
        chat_id: int,
        target: str | None = None,
        limit: int = 10,
    ) -> list[MemoryItem]:
        """
        Получить релевантную память СТРОГО ИЗ ОДНОГО ЧАТА.
        Память из других чатов НЕ включается — КОНФИДЕНЦИАЛЬНО.
        """
        from sqlalchemy import select, and_
        from src.database.session import get_session

        async for session in get_session():
            # СТРОГИЙ ФИЛЬТР: только текущий чат
            conditions = [
                MemoryItem.chat_id == chat_id,  # ← ТОЛЬКО ЭТОТ ЧАТ
                MemoryItem.type.in_([
                    MemoryType.FACT,
                    MemoryType.PREFERENCE,
                    MemoryType.EVENT,
                    MemoryType.RELATIONSHIP,
                    MemoryType.GROUP_RULE,
                ]),
            ]

            # Если есть target — ищем по контенту
            if target:
                conditions.append(
                    MemoryItem.content.ilike(f"%{target}%")
                )

            stmt = (
                select(MemoryItem)
                .where(and_(*conditions))
                .order_by(MemoryItem.relevance.desc(), MemoryItem.confidence.desc())
                .limit(limit)
            )

            result = await session.execute(stmt)
            return list(result.scalars().all())

    def _confidence_to_level(self, confidence: float) -> ConfidenceLevel:
        """Конвертировать confidence score в уровень знания."""
        if confidence >= 0.7:
            return ConfidenceLevel.CERTAIN
        elif confidence >= 0.4:
            return ConfidenceLevel.UNCERTAIN
        return ConfidenceLevel.UNKNOWN

    def _generate_clarification(self, topic: str | None, target: str | None) -> str | None:
        """Сгенерировать запрос уточнения."""
        if target and topic == "preference":
            return f"Я не помню, чтобы {target} говорил(а) о своих предпочтениях. Может я что-то пропустил?"
        if target and topic == "fact":
            return f"У меня нет информации про {target}. Я мог пропустить это!"
        if target and topic == "relationship":
            return f"Я не знаю про связи {target}. Мы об этом не говорили?"
        if target:
            return f"Я мало знаю про {target}. Расскажешь?"

        return "У меня мало данных по этому вопросу."


knowledge_analyzer = KnowledgeAnalyzer()
