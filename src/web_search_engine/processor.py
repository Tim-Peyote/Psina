"""
Search Processor — обработка результатов поиска.

1. Берёт сырые результаты поиска
2. Чистит и лимитирует по токенам
3. Скармливает LLM для формирования ответа
4. Возвращает ответ с источником
"""

import structlog

from src.config import settings
from src.llm_adapter.base import LLMProvider, to_openai_messages
from src.web_search_engine.search_provider import SearchResult
from src.web_search_engine.cache import search_cache
from src.web_search_engine.search_provider import search_provider

logger = structlog.get_logger()


class SearchProcessor:
    """Обработка результатов веб-поиска."""

    def __init__(self) -> None:
        self._llm = LLMProvider.get_provider()
        self._hourly_count: list[float] = []
        self._daily_count: list[float] = []

    async def search_and_answer(self, query: str) -> str:
        """
        Выполнить поиск и сформировать ответ через LLM.
        """
        logger.info("Search requested", query=query)

        # Проверяем кеш
        cached = search_cache.get(query)
        if cached:
            logger.debug("Using cached search results", query=query)
            return await self._generate_answer(query, cached)

        # Проверяем лимиты
        if not settings.web_search_unlimited:
            if not self._check_limits():
                logger.warning("Search rate limit exceeded", query=query)
                return "Извини, я исчерпал лимит поиска на сегодня. Попробуй позже."

        # Ищем
        logger.debug("Executing web search", query=query, provider=search_provider.get_name())
        results = await search_provider.search(query, max_results=settings.web_search_max_results)

        if not results:
            logger.info("No search results found", query=query)
            return f"Не нашёл ничего по запросу «{query}». Попробуй переформулировать."

        # Кеш
        search_cache.put(query, results)

        # Считаем
        self._record_usage()

        # Формируем ответ
        logger.debug("Generating LLM answer from search results", query=query, results_count=len(results))
        return await self._generate_answer(query, results)

    async def _generate_answer(self, query: str, results: list[SearchResult]) -> str:
        """Сформировать ответ через LLM."""
        # Формируем контекст из результатов
        context_parts = []
        for i, r in enumerate(results, 1):
            context_parts.append(
                f"[{i}] {r.title}\n{r.snippet}\nИсточник: {r.url}"
            )

        search_context = "\n\n".join(context_parts)

        system_prompt = (
            "Ты — Псина, участник чата. "
            "Тебе дали результаты поиска по запросу пользователя. "
            "Ответь кратко и по делу, используя эти результаты. "
            "Если результаты противоречивы — скажи об этом. "
            "В конце укажи источник(ы) — DuckDuckGo. "
            "НЕ выдумывай информацию которой нет в результатах поиска."
        )

        user_message = (
            f"Запрос: {query}\n\n"
            f"Результаты поиска:\n{search_context}\n\n"
            f"Ответь на запрос, используя эти результаты."
        )

        messages = to_openai_messages(system_prompt, [{"role": "user", "content": user_message}])

        try:
            response = await self._llm.generate_response(messages=messages)
            return response
        except Exception as e:
            logger.error("Failed to generate search answer", error=str(e))
            # Fallback — просто вернём сырые результаты
            snippets = [f"• {r.snippet}" for r in results[:3]]
            return f"По запросу «{query}»:\n\n" + "\n".join(snippets) + f"\n\n(Источник: {results[0].source if results else 'web'})"

    def _check_limits(self) -> bool:
        """Проверить лимиты."""
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)

        # Hourly
        hour_ago = now - timedelta(hours=1)
        self._hourly_count = [t for t in self._hourly_count if t > hour_ago.timestamp()]
        if len(self._hourly_count) >= settings.web_search_max_per_hour:
            return False

        # Daily
        day_ago = now - timedelta(days=1)
        self._daily_count = [t for t in self._daily_count if t > day_ago.timestamp()]
        if len(self._daily_count) >= settings.web_search_max_per_day:
            return False

        return True

    def _record_usage(self) -> None:
        """Записать использование."""
        import time
        now = time.time()
        self._hourly_count.append(now)
        self._daily_count.append(now)

    def get_usage_stats(self) -> dict:
        """Получить статистику поиска."""
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        hour_ago = now - timedelta(hours=1)
        day_ago = now - timedelta(days=1)

        import time
        hourly = len([t for t in self._hourly_count if t > hour_ago.timestamp()])
        daily = len([t for t in self._daily_count if t > day_ago.timestamp()])

        return {
            "hourly_count": hourly,
            "hourly_limit": settings.web_search_max_per_hour,
            "daily_count": daily,
            "daily_limit": settings.web_search_max_per_day,
            "unlimited": settings.web_search_unlimited,
        }


search_processor = SearchProcessor()
