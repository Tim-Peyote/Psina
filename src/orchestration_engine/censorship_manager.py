"""
Censorship Manager — уровень цензуры определяют пользователи.

3 уровня:
- strict: фильтрует мат и грубости
- moderate: допускает лёгкий мат
- free: без ограничений

Управление обычной речью:
- "убери цензуру" / "не фильтруй" / "без фильтров"
- "пофильтруй" / "цензура" / "будь аккуратнее"
"""

from enum import Enum

import structlog

from src.config import settings

logger = structlog.get_logger()


class CensorshipLevel(Enum):
    STRICT = "strict"
    MODERATE = "moderate"
    FREE = "free"


class CensorshipManager:
    """
    Управляет уровнем цензуры для каждого чата.
    """

    def __init__(self) -> None:
        # chat_id -> CensorshipLevel
        self._levels: dict[int, CensorshipLevel] = {}
        # Default from env
        default = getattr(settings, "default_censorship_level", "moderate")
        try:
            self._default = CensorshipLevel(default)
        except ValueError:
            self._default = CensorshipLevel.MODERATE

    def get_level(self, chat_id: int) -> CensorshipLevel:
        """Получить уровень цензуры для чата."""
        return self._levels.get(chat_id, self._default)

    def set_level(self, chat_id: int, level: CensorshipLevel) -> None:
        """Установить уровень цензуры."""
        self._levels[chat_id] = level
        logger.info("Censorship level changed", chat_id=chat_id, level=level.value)

    def parse_level_from_text(self, text: str) -> CensorshipLevel | None:
        """
        Попытаться определить уровень цензуры из текста.
        """
        text_lower = text.lower()

        # Free level
        free_triggers = [
            "убери цензуру", "не фильтруй", "без фильтров", "без цензуры",
            "не фильтруй базар", "будь без фильтров", "фильтр выкл",
            "отключи цензуру", "выключи цензуру", "отключи фильтр",
            "не сдерживайся", "без ограничений", "полный текст",
        ]
        if any(t in text_lower for t in free_triggers):
            return CensorshipLevel.FREE

        # Strict level
        strict_triggers = [
            "пофильтруй", "цензура", "будь аккуратнее", "фильтруй",
            "без мата", "не матюкайся", "цензура вкл", "фильтр вкл",
            "включи цензуру", "включи фильтр", "сдерживайся",
            "будь аккуратнее с выражениями", "поприличнее",
        ]
        if any(t in text_lower for t in strict_triggers):
            return CensorshipLevel.STRICT

        # Moderate (default)
        moderate_triggers = [
            "умеренная цензура", "средний фильтр", "как обычно",
            "обычный режим", "по умолчанию",
        ]
        if any(t in text_lower for t in moderate_triggers):
            return CensorshipLevel.MODERATE

        return None

    def get_instruction_for_llm(self, chat_id: int) -> str:
        """
        Получить инструкцию для LLM по цензуре.
        """
        level = self.get_level(chat_id)

        if level == CensorshipLevel.STRICT:
            return (
                "РЕЖИМ ЦЕНЗУРЫ: СТРОГИЙ.\n"
                "Не используй мат и грубые выражения. "
                "Будь вежлив и аккуратен в формулировках. "
                "Если хочешь выразить эмоцию — делай это мягко."
            )
        elif level == CensorshipLevel.MODERATE:
            return (
                "РЕЖИМ ЦЕНЗУРЫ: УМЕРЕННЫЙ.\n"
                "Можешь использовать лёгкие выражения, но не переборщи. "
                "Грубый мат не используй. Обычные эмоции — ок."
            )
        else:  # FREE
            return (
                "РЕЖИМ ЦЕНЗУРЫ: СВОБОДНЫЙ.\n"
                "Можешь общаться без ограничений. Мат, грубые выражения, "
                "сарказм — всё ок. Пользователи этого чата сами определили "
                "что такой уровень общения приемлем. Не фильтруй себя."
            )


censorship_manager = CensorshipManager()
