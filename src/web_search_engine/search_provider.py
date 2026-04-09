"""
Search Providers — реестр поисковых провайдеров.

Провайдеры:
- duckduckgo: DuckDuckGo HTML (httpx + BeautifulSoup)
- mock: заглушка для разработки
"""

import structlog

from src.config import settings
from src.web_search_engine.duckduckgo_provider import DuckDuckGoProvider, MockDuckDuckGoProvider

logger = structlog.get_logger()


class SearchRegistry:
    """Реестр поисковых провайдеров."""

    _ddg_provider: DuckDuckGoProvider | None = None
    _mock_provider: MockDuckDuckGoProvider | None = None

    @classmethod
    def get_ddg(cls) -> DuckDuckGoProvider:
        if cls._ddg_provider is None:
            cls._ddg_provider = DuckDuckGoProvider()
        return cls._ddg_provider

    @classmethod
    def get_mock(cls) -> MockDuckDuckGoProvider:
        if cls._mock_provider is None:
            cls._mock_provider = MockDuckDuckGoProvider()
        return cls._mock_provider

    @classmethod
    def get_provider(cls):
        """Получить провайдер согласно настройкам."""
        provider_name = settings.web_search_provider if hasattr(settings, "web_search_provider") else "duckduckgo"

        if provider_name == "duckduckgo":
            return cls.get_ddg()
        elif provider_name == "mock":
            return cls.get_mock()
        else:
            logger.warning("Unknown search provider, falling back to duckduckgo", provider=provider_name)
            return cls.get_ddg()


# Singleton
search_provider = SearchRegistry.get_provider()
