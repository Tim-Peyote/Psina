"""
DuckDuckGo HTML Provider — поиск через HTML версию DuckDuckGo.

Использует httpx + BeautifulSoup для парсинга html.duckduckgo.com.
Никаких API ключей, captcha, дополнительных контейнеров.
"""

import httpx
import structlog
from bs4 import BeautifulSoup

from src.config import settings
from src.web_search_engine.search_provider_base import BaseSearchProvider, SearchResult

logger = structlog.get_logger()

DDG_HTML_URL = "https://html.duckduckgo.com/html/"


class DuckDuckGoProvider(BaseSearchProvider):
    """Поиск через DuckDuckGo HTML."""

    def __init__(self) -> None:
        self._timeout = settings.chrome_timeout if hasattr(settings, "chrome_timeout") else 30

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        max_results = min(max_results, settings.web_search_max_results)

        logger.info("DuckDuckGo HTML search start", query=query, max_results=max_results)

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.5",
                },
            ) as client:
                params = {"q": query}
                resp = await client.get(DDG_HTML_URL, params=params)
                resp.raise_for_status()

                soup = BeautifulSoup(resp.text, "html.parser")
                results = []

                for div in soup.select("div.result")[:max_results]:
                    title_tag = div.select_one("a.result__a")
                    snippet_tag = div.select_one("a.result__snippet")

                    if not title_tag or not snippet_tag:
                        continue

                    title = title_tag.get_text(strip=True)
                    url = title_tag.get("href", "")
                    # DuckDuckGo оборачивает URL в редирект, извлекаем настоящий
                    if url.startswith("/uddg?"):
                        from urllib.parse import parse_qs, urlparse
                        parsed = urlparse(url)
                        qs = parse_qs(parsed.query)
                        if "uddg" in qs:
                            url = qs["uddg"][0]

                    snippet = snippet_tag.get_text(strip=True)

                    if title and url:
                        results.append(
                            SearchResult(
                                title=title,
                                url=url,
                                snippet=snippet,
                                source="DuckDuckGo",
                            )
                        )

                if results:
                    logger.info("DuckDuckGo search done", query=query, results=len(results))
                else:
                    logger.warning("DuckDuckGo no results", query=query)

                return results

        except httpx.TimeoutException:
            logger.error("DuckDuckGo timeout", query=query, timeout=self._timeout)
            return []
        except httpx.HTTPStatusError as e:
            logger.error("DuckDuckGo HTTP error", query=query, status=e.response.status_code)
            return []
        except Exception as e:
            logger.error("DuckDuckGo search failed", query=query, error=type(e).__name__, details=str(e))
            return []

    def get_name(self) -> str:
        return "DuckDuckGo HTML"


class MockDuckDuckGoProvider(BaseSearchProvider):
    """Моковый провайдер для разработки."""

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        return [
            SearchResult(
                title=f"[Mock] {query}",
                url="https://mock.example",
                snippet=f"Моковый результат для: {query}",
                source="Mock",
            )
        ]

    def get_name(self) -> str:
        return "Mock DuckDuckGo"
