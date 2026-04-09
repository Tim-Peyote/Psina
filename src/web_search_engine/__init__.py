"""
Web Search Engine — поиск через SearXNG.

Пайплайн:
  message → intent_detector → search_processor → SearXNGProvider → LLM → ответ

Компоненты:
- search_provider: SearXNG provider (единственный)
- processor: пайплайн поиска + LLM ответ
- intent_detector: детекция когда нужен поиск
- cache: кеш результатов (in-memory, TTL)
- page_fetcher: загрузка полной страницы по URL (httpx)
"""

from src.web_search_engine.search_provider_base import SearchResult, BaseSearchProvider
from src.web_search_engine.search_provider import search_provider, SearchRegistry
from src.web_search_engine.processor import search_processor
from src.web_search_engine.intent_detector import search_intent_detector
from src.web_search_engine.page_fetcher import page_fetcher, WebPageFetcher

__all__ = [
    "SearchResult",
    "BaseSearchProvider",
    "search_provider",
    "SearchRegistry",
    "search_processor",
    "search_intent_detector",
    "page_fetcher",
    "WebPageFetcher",
]
