"""
Search Cache — кеш результатов поиска.

Хранит результаты в памяти с TTL (5 минут по умолчанию).
Чтобы не искать одно и то же много раз подряд.
"""

from datetime import datetime, timedelta, timezone

import structlog

from src.config import settings
from src.web_search_engine.search_provider import SearchResult

logger = structlog.get_logger()


class SearchCache:
    """In-memory кеш результатов поиска."""

    def __init__(self) -> None:
        # query -> (results, expires_at)
        self._cache: dict[str, tuple[list[SearchResult], datetime]] = {}
        self._ttl = settings.web_search_cache_ttl

    def get(self, query: str) -> list[SearchResult] | None:
        """Получить из кеша."""
        entry = self._cache.get(query)
        if entry is None:
            return None

        results, expires_at = entry
        if datetime.now(timezone.utc) > expires_at:
            del self._cache[query]
            return None

        logger.debug("Search cache hit", query=query)
        return results

    def put(self, query: str, results: list[SearchResult]) -> None:
        """Положить в кеш."""
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=self._ttl)
        self._cache[query] = (results, expires_at)
        logger.debug("Search cache updated", query=query, ttl=self._ttl)

    def clear(self) -> None:
        """Очистить кеш."""
        self._cache.clear()


search_cache = SearchCache()
