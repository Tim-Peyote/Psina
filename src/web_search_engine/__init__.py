"""
Web Search Engine — поиск через DuckDuckGo HTML.

Пайплайн:
  LLM router → search_processor → DuckDuckGoProvider → LLM → ответ

Компоненты:
- duckduckgo_provider: httpx GET к html.duckduckgo.com + BeautifulSoup
- search_provider: реестр провайдеров
- processor: пайплайн поиска + LLM ответ
- cache: кеш результатов (in-memory, TTL)
- page_fetcher: загрузка полной страницы по URL (httpx)

Примечание: intent_detector удалён — маршрутизацию теперь делает LLM-роутер.
"""

from src.web_search_engine.search_provider_base import SearchResult, BaseSearchProvider
from src.web_search_engine.search_provider import search_provider, SearchRegistry
from src.web_search_engine.processor import search_processor
from src.web_search_engine.page_fetcher import page_fetcher, WebPageFetcher

__all__ = [
    "SearchResult",
    "BaseSearchProvider",
    "search_provider",
    "SearchRegistry",
    "search_processor",
    "page_fetcher",
    "WebPageFetcher",
]
