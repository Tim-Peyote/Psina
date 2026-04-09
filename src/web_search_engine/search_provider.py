"""
Search Providers — реестр поисковых провайдеров.

Единственный провайдер: SearXNG.
"""

import structlog

from src.config import settings
from src.web_search_engine.searxng_provider import SearXNGProvider

logger = structlog.get_logger()


class SearchRegistry:
    """Реестр поисковых провайдеров."""

    _provider: SearXNGProvider | None = None

    @classmethod
    def get_provider(cls) -> SearXNGProvider:
        if cls._provider is None:
            cls._provider = SearXNGProvider()
        return cls._provider


# Singleton
search_provider = SearchRegistry.get_provider()
