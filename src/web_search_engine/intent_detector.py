"""
Intent Detector — определяет когда пользователю нужен ответ из интернета.

Паттерны:
- Погода: "погода", "температура", "дождь"
- Новости: "что случилось", "новости", "что произошло"
- Курсы: "курс доллара", "биткоин", "акции"
- Спорт: "кто выиграл", "матч", "счёт"
- Факты: "кто такой", "что такое", "когда был" (актуальные)
"""

import re
from dataclasses import dataclass

import structlog

from src.config import settings

logger = structlog.get_logger()


@dataclass
class SearchIntent:
    is_search_needed: bool
    confidence: float
    query: str
    reason: str


class SearchIntentDetector:
    """
    Определяет нужен ли поиск в интернете.
    """

    def __init__(self) -> None:
        # Паттерны которые точно требуют поиска
        self._search_patterns: list[tuple[str, re.Pattern, float]] = [
            # Погода
            ("weather", re.compile(r'(?:какая|какой|какое)\s+(?:сейчас\s+)?(?:погода|температура|дождь|снег|ветер)', re.IGNORECASE), 0.9),
            ("weather_city", re.compile(r'(?:погода|температура)\s+(?:в|на)\s+([А-ЯA-Z][а-яa-z]+)', re.IGNORECASE), 0.95),

            # Новости/события
            ("news", re.compile(r'(?:что\s+(?:случилось|произошло|нового)|какие\s+новости|что\s+в\s+мире)', re.IGNORECASE), 0.85),

            # Курсы/финансы
            ("crypto", re.compile(r'(?:курс|цена|стоит)\s+(?:биткоин|bitcoin|ethereum|эфириум|крипта)', re.IGNORECASE), 0.9),
            ("currency", re.compile(r'(?:курс|сколько\s+стоит)\s+(?:доллар|евро|рубль|фунт|юань)', re.IGNORECASE), 0.9),
            ("stocks", re.compile(r'(?:акции|котировки|индекс)\s+([А-ЯA-Z]+)', re.IGNORECASE), 0.85),

            # Спорт
            ("sport_result", re.compile(r'(?:кто\s+выиграл|результат|счёт|матч)\s+([А-ЯA-Zа-яa-z]+)', re.IGNORECASE), 0.9),

            # Актуальные факты
            ("current_event", re.compile(r'(?:кто\s+(?:сейчас|теперь|в\s+(?:этом|текущем))\s+(?:президент|владелец|директор|глава))', re.IGNORECASE), 0.8),

            # Время
            ("time_query", re.compile(r'(?:который\s+час|сколько\s+времени|какое\s+время)\s+(?:в|на)\s+([А-ЯA-Zа-яa-z]+)', re.IGNORECASE), 0.85),

            # Поиск "найди", "поищи"
            ("explicit_search", re.compile(r'(?:найди|поищи|погугли|загугли|гугл)\s+(.+)', re.IGNORECASE), 0.95),
        ]

        # Паттерны которые НЕ требуют поиска (чтобы не путать с поиском)
        self._not_search_patterns = [
            re.compile(r'(?:помнишь|помню|говорил|сказал)\s+(?:вчера|недавно|раньше|в прошлый)', re.IGNORECASE),  # память
            re.compile(r'(?:помнишь|вспоминаешь)\s+(?:мы|ты|я)', re.IGNORECASE),  # воспоминания
            re.compile(r'(?:кто\s+(?:из\s+нас|в\s+чате|в\s+нашей))', re.IGNORECASE),  # про чат
        ]

    def detect(self, text: str) -> SearchIntent:
        """
        Определить нужен ли поиск.
        """
        if not settings.web_search_enabled:
            return SearchIntent(
                is_search_needed=False,
                confidence=0.0,
                query="",
                reason="web_search_disabled",
            )

        # Проверяем что это НЕ память
        for pattern in self._not_search_patterns:
            if pattern.search(text):
                return SearchIntent(
                    is_search_needed=False,
                    confidence=0.0,
                    query="",
                    reason="memory_query",
                )

        # Ищем совпадения
        best_score = 0.0
        best_query = ""
        best_reason = ""

        for reason, pattern, base_score in self._search_patterns:
            match = pattern.search(text)
            if match:
                # Извлекаем query из группы или всего текста
                query = match.group(1) if match.lastindex else text.strip()
                if len(query) > base_score:
                    base_score = min(base_score, 0.95)

                if base_score > best_score:
                    best_score = base_score
                    best_query = query
                    best_reason = reason

        return SearchIntent(
            is_search_needed=best_score >= 0.7,
            confidence=best_score,
            query=best_query,
            reason=best_reason,
        )


search_intent_detector = SearchIntentDetector()
