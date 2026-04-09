"""
Search Cache — кеш результатов поиска.

Хранит результаты в памяти с TTL (5 минут по умолчанию).
Чтобы не искать одно и то же много раз подряд.
"""

from datetime import datetime, timedelta, timezone

import structlog

from src.config import settings
from src.web_search_engine.search_provider_base import SearchResult

logger = structlog.get_logger()


class SearchCache:
    """In-memory кеш результатов поиска."""

    _MAX_SIZE = 500  # Максимум записей в кэше

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
        # Очистка при превышении размера
        if len(self._cache) >= self._MAX_SIZE:
            self._evict_expired()
            # Если всё ещё превышение — удаляем половину oldest
            if len(self._cache) >= self._MAX_SIZE:
                sorted_items = sorted(self._cache.items(), key=lambda x: x[1][1])
                for key, _ in sorted_items[:len(sorted_items) // 2]:
                    del self._cache[key]
                logger.info("Cache evicted", old_size=len(sorted_items), new_size=len(self._cache))

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=self._ttl)
        self._cache[query] = (results, expires_at)
        logger.debug("Search cache updated", query=query, ttl=self._ttl, size=len(self._cache))

    def clear(self) -> None:
        """Очистить кеш."""
        self._cache.clear()

    def _evict_expired(self) -> None:
        """Удалить все протухшие записи."""
        now = datetime.now(timezone.utc)
        expired = [k for k, (_, exp) in self._cache.items() if now > exp]
        for k in expired:
            del self._cache[k]
        if expired:
            logger.debug("Expired cache entries evicted", count=len(expired))


search_cache = SearchCache()
