"""
Search Processor — пайплайн поиска.

1. Проверяет кеш
2. Проверяет лимиты
3. Запрашивает DuckDuckGo
4. Формирует ответ через LLM
"""

import time
from datetime import datetime, timedelta, timezone

import structlog

from src.config import settings
from src.llm_adapter.base import LLMProvider, to_openai_messages
from src.web_search_engine.search_provider_base import SearchResult
from src.web_search_engine.cache import search_cache
from src.web_search_engine.search_provider import search_provider
from src.telegram_gateway.message_postprocessor import message_postprocessor

logger = structlog.get_logger()


class SearchProcessor:
    """Обработка результатов веб-поиска."""

    def __init__(self) -> None:
        self._llm = LLMProvider.get_provider()
        self._hourly_count: list[float] = []
        self._daily_count: list[float] = []

    async def search_and_answer(self, query: str) -> str:
        """Выполнить поиск и сформировать ответ через LLM."""
        logger.info("Search requested", query=query)

        # Проверяем кеш
        cached = search_cache.get(query)
        if cached:
            logger.info("Search cache hit", query=query, results=len(cached))
            return await self._generate_answer(query, cached)

        # Проверяем лимиты
        if not settings.web_search_unlimited:
            if not self._check_limits():
                logger.warning("Search rate limit exceeded", query=query)
                return "Извини, я исчерпал лимит поиска на сегодня. Попробуй позже."

        # Ищем
        logger.info("Calling search engine", query=query, provider=search_provider.get_name())
        results = await search_provider.search(query, max_results=settings.web_search_max_results)

        if not results:
            logger.warning("Search returned no results", query=query)
            return f"Не нашёл ничего по запросу «{query}». Попробуй переформулировать."

        # Кеш
        search_cache.put(query, results)
        self._record_usage()

        # Формируем ответ
        logger.info("Generating LLM answer", query=query, results_count=len(results))
        return await self._generate_answer(query, results)

    def _detect_query_type(self, query: str) -> str:
        """Определить тип запроса для подбора шаблона форматирования."""
        q = query.lower()
        if any(w in q for w in ["цена", "стоит", "курс", "сколько", "прайс", "тариф", "стоимость", "цены", "купить"]):
            return "price"
        if any(w in q for w in ["новости", "что случилось", "когда", "последние", "сегодня", "вчера", "сейчас"]):
            return "news"
        if any(w in q for w in ["как ", "как сделать", "инструкция", "способ", "метод", "как установить", "как настроить"]):
            return "howto"
        return "facts"

    async def _generate_answer(self, query: str, results: list[SearchResult]) -> str:
        """Сформировать структурированный ответ через LLM."""
        context_parts = []
        for i, r in enumerate(results, 1):
            context_parts.append(
                f"[{i}] {r.title}\n{r.snippet}\nURL: {r.url}"
            )

        search_context = "\n\n".join(context_parts)

        logger.debug(
            "Built search context",
            query=query,
            results_count=len(results),
            context_preview=search_context[:200],
        )

        query_type = self._detect_query_type(query)

        type_hints = {
            "price": (
                "Запрос о ценах/стоимости. Формат ответа:\n"
                "- Первая строка: главная цифра жирным: <b>X ₽</b> или <b>$X</b>\n"
                "- Bullet-пункты для дополнительных цифр (макс 3)\n"
                "- Укажи дату актуальности если есть\n"
            ),
            "news": (
                "Запрос о событиях/новостях. Формат ответа:\n"
                "- Первая строка: главный факт с датой если известна\n"
                "- Bullet-пункты для деталей (макс 3)\n"
                "- Ссылки на источники обязательны\n"
            ),
            "howto": (
                "Запрос-инструкция. Формат ответа:\n"
                "- Краткий ответ (что нужно сделать)\n"
                "- Нумерованные шаги если нужно (макс 4)\n"
                "- Ссылка на подробную инструкцию\n"
            ),
            "facts": (
                "Фактический запрос. Формат ответа:\n"
                "- Первая строка: прямой ответ (1-2 предложения) с <b>ключевыми фактами</b> жирным\n"
                "- Bullet-пункты для важных деталей (макс 4)\n"
            ),
        }

        now_str = datetime.now(timezone.utc).strftime("%d.%m.%Y")
        system_prompt = (
            f"Сегодня {now_str}.\n"
            "Ты — Бот, участник чата. "
            "Ты УЖЕ нашёл информацию в интернете — отвечай уверенно. "
            "НЕ выдумывай — используй только данные из результатов поиска. "
            "НЕ используй маркеры [1], [2], 【1】 — они некрасивы. "
            "НЕ пиши «по данным источников» и подобные клише.\n\n"
            f"{type_hints.get(query_type, type_hints['facts'])}\n"
            "ССЫЛКИ (обязательный раздел в конце):\n"
            "- Добавь раздел <b>Источники:</b> с нумерованными ссылками\n"
            "- Формат: 1. <a href=\"ПОЛНЫЙ_URL\">Название страницы</a>\n"
            "- Используй полный URL, НЕ обрезай до домена\n"
            "- Максимум 3 источника"
        )

        user_message = (
            f"Запрос: {query}\n\n"
            f"Результаты поиска:\n{search_context}\n\n"
            f"Дай структурированный ответ."
        )

        messages = to_openai_messages(system_prompt, [{"role": "user", "content": user_message}])

        try:
            response = await self._llm.generate_response(messages=messages)
            logger.info("Search answer generated", query=query, answer_len=len(response))
            return message_postprocessor.process(response)
        except Exception as e:
            logger.error(
                "LLM failed to generate search answer, returning raw results",
                query=query,
                error=type(e).__name__,
                details=str(e),
            )
            # Fallback — просто вернём сырые результаты
            snippets = [f"• {r.snippet}" for r in results[:3]]
            links = "\n".join(
                f"• <a href=\"{r.url}\">{r.title}</a>"
                for r in results[:3]
            )
            fallback = f"По запросу «{query}»:\n\n" + "\n".join(snippets) + f"\n\n<b>Источники:</b>\n{links}"
            return message_postprocessor.process(fallback)

    def _check_limits(self) -> bool:
        """Проверить лимиты."""
        now = datetime.now(timezone.utc)

        hour_ago = now - timedelta(hours=1)
        self._hourly_count = [t for t in self._hourly_count if t > hour_ago.timestamp()]
        if len(self._hourly_count) >= settings.web_search_max_per_hour:
            logger.warning("Hourly search limit reached", count=len(self._hourly_count), limit=settings.web_search_max_per_hour)
            return False

        day_ago = now - timedelta(days=1)
        self._daily_count = [t for t in self._daily_count if t > day_ago.timestamp()]
        if len(self._daily_count) >= settings.web_search_max_per_day:
            logger.warning("Daily search limit reached", count=len(self._daily_count), limit=settings.web_search_max_per_day)
            return False

        return True

    def _record_usage(self) -> None:
        """Записать использование."""
        now = time.time()
        self._hourly_count.append(now)
        self._daily_count.append(now)

    def get_usage_stats(self) -> dict:
        """Получить статистику поиска."""
        now = datetime.now(timezone.utc)
        hour_ago = now - timedelta(hours=1)
        day_ago = now - timedelta(days=1)

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
