"""
Search Providers — абстракция и реализации для веб-поиска.

Провайдеры:
- DuckDuckGo (бесплатный, без API ключа)
- Mock (для разработки)
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

import structlog

from src.config import settings

logger = structlog.get_logger()


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    source: str


class BaseSearchProvider(ABC):
    """Абстрактный интерфейс поиска."""

    @abstractmethod
    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        """Выполнить поиск."""
        ...

    @abstractmethod
    def get_name(self) -> str:
        """Название провайдера."""
        ...


class DuckDuckGoProvider(BaseSearchProvider):
    """
    Бесплатный поиск через DuckDuckGo.
    Использует пакет duckduckgo_search.
    """

    def __init__(self) -> None:
        from duckduckgo_search import DDGS
        self._ddgs = DDGS()

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        max_results = min(max_results, settings.web_search_max_results)

        try:
            results = self._ddgs.text(query, max_results=max_results)

            search_results = []
            for r in results:
                search_results.append(SearchResult(
                    title=r.get("title", ""),
                    url=r.get("href", ""),
                    snippet=r.get("body", ""),
                    source="DuckDuckGo",
                ))

            logger.debug("DuckDuckGo search completed", query=query, results=len(search_results))
            return search_results

        except Exception as e:
            logger.error("DuckDuckGo search failed", query=query, error=str(e))
            return []

    def get_name(self) -> str:
        return "DuckDuckGo"


class MockSearchProvider(BaseSearchProvider):
    """Моковый поиск для разработки."""

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        return [
            SearchResult(
                title=f"[Mock] {query}",
                url="https://mock.example",
                snippet=f"Это моковый результат для запроса: {query}",
                source="Mock",
            )
        ]

    def get_name(self) -> str:
        return "Mock"


class SearchRegistry:
    """Реестр поисковых провайдеров."""

    _providers: dict[str, BaseSearchProvider] = {
        "duckduckgo": DuckDuckGoProvider(),
        "mock": MockSearchProvider(),
    }

    @classmethod
    def get_provider(cls) -> BaseSearchProvider:
        # По умолчанию DuckDuckGo
        return cls._providers.get("duckduckgo", cls._providers["mock"])

    @classmethod
    def get_mock(cls) -> BaseSearchProvider:
        return cls._providers["mock"]


search_provider = SearchRegistry.get_provider()
