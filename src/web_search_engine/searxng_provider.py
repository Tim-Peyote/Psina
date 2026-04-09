"""
SearXNG Search Provider — поиск через self-hosted мета-поисковик.

SearXNG — free internet metasearch engine.
- Не требует API ключа
- Не блокирует captcha
- Агрегирует результаты из 70+ поисковиков
- Ставится в Docker

URL: http://searxng:8080 (внутри Docker compose)
"""

import httpx
import structlog

from src.config import settings
from src.web_search_engine.search_provider_base import BaseSearchProvider, SearchResult

logger = structlog.get_logger()


class SearXNGProvider(BaseSearchProvider):
    """Поиск через SearXNG instance."""

    def __init__(self) -> None:
        self._base_url = settings.searxng_url
        self._timeout = 30

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        max_results = min(max_results, settings.web_search_max_results)

        logger.info("SearXNG search start", query=query, max_results=max_results, url=self._base_url)

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                url = f"{self._base_url}/search"
                params = {
                    "q": query,
                    "format": "json",
                    "categories": "general",
                    "language": "ru",
                    "pageno": 1,
                }

                logger.debug("SearXNG request", url=url, params=params)
                resp = await client.get(url, params=params)
                resp.raise_for_status()

                data = resp.json()
                raw_results = data.get("results", [])
                logger.info("SearXNG response", query=query, total_raw=len(raw_results))

                results = []
                for item in raw_results[:max_results]:
                    title = item.get("title", "")
                    url = item.get("url", "")
                    snippet = item.get("content", "")
                    engine = item.get("engine", "unknown")

                    if not title or not url:
                        logger.debug("Skipping incomplete result", title=title, url=url)
                        continue

                    results.append(
                        SearchResult(
                            title=title.strip(),
                            url=url,
                            snippet=snippet.strip(),
                            source=f"SearXNG ({engine})",
                        )
                    )

                if results:
                    logger.info(
                        "SearXNG search done",
                        query=query,
                        results=len(results),
                        engines=list(set(r.source for r in results)),
                    )
                else:
                    logger.warning("SearXNG no valid results", query=query)

                return results

        except httpx.ConnectError as e:
            logger.error(
                "SearXNG connection failed — is SearXNG container running?",
                url=self._base_url,
                error=str(e),
            )
            return []
        except httpx.TimeoutException:
            logger.error("SearXNG timeout", url=self._base_url, timeout=self._timeout)
            return []
        except httpx.HTTPStatusError as e:
            logger.error("SearXNG HTTP error", url=self._base_url, status=e.response.status_code, error=str(e))
            return []
        except Exception as e:
            logger.error("SearXNG unexpected error", query=query, error=type(e).__name__, details=str(e))
            return []

    def get_name(self) -> str:
        return f"SearXNG ({self._base_url})"
