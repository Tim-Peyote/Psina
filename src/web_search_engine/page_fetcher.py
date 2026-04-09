"""
Page Fetcher — загрузка и очистка веб-страниц.

Использует httpx для загрузки страниц и простую эвристику для извлечения текста.
"""

import asyncio
import re
from dataclasses import dataclass
from typing import Optional

import httpx
import structlog

from src.config import settings

logger = structlog.get_logger()

# Примерное кол-во символов на токен (грубая оценка)
CHARS_PER_TOKEN = 4


@dataclass
class FetchedPage:
    """Результат загрузки страницы."""

    url: str
    title: str
    text: str
    success: bool
    error: Optional[str] = None


class WebPageFetcher:
    """
    Загрузка и очистка веб-страниц для LLM.

    Использование:
        fetcher = WebPageFetcher()
        page = await fetcher.fetch("https://example.com/article")
        if page.success:
            print(page.text)  # чистый текст для LLM
    """

    def __init__(self) -> None:
        self._timeout = settings.chrome_timeout if hasattr(settings, "chrome_timeout") else 30
        self._max_tokens = settings.max_context_tokens if hasattr(settings, "max_context_tokens") else 3000

    async def fetch(self, url: str, max_tokens: Optional[int] = None) -> FetchedPage:
        """
        Загрузить страницу и извлечь читаемый текст.

        Args:
            url: URL страницы
            max_tokens: переопределить лимит токенов
        """
        max_chars = (max_tokens or self._max_tokens) * CHARS_PER_TOKEN

        logger.info("Fetching page", url=url, max_chars=max_chars)

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                },
            ) as client:
                resp = await client.get(url)

                if resp.status_code >= 400:
                    return FetchedPage(
                        url=url,
                        title="",
                        text="",
                        success=False,
                        error=f"HTTP {resp.status_code}",
                    )

                html = resp.text

                # Извлекаем заголовок
                title = self._extract_title(html)

                # Извлекаем текст
                text = self._html_to_text(html)

                # Обрезаем по лимиту
                if len(text) > max_chars:
                    text = text[:max_chars] + "\n\n... [текст обрезан по лимиту]"

                logger.info("Page fetched successfully", url=url, title=title, text_len=len(text))
                return FetchedPage(
                    url=url,
                    title=title,
                    text=text,
                    success=True,
                )

        except Exception as e:
            logger.error("Failed to fetch page", url=url, error=str(e))
            return FetchedPage(
                url=url,
                title="",
                text="",
                success=False,
                error=str(e),
            )

    def _extract_title(self, html: str) -> str:
        """Извлечь заголовок из HTML."""
        match = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
        if match:
            return match.group(1).strip()
        return ""

    def _html_to_text(self, html: str) -> str:
        """
        Простое преобразование HTML в текст.
        Убирает теги, скрипты, стили.
        """
        # Удаляем скрипты и стили
        html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)

        # Заменяем блоки на переносы строк
        for tag in ["</p>", "</div>", "</li>", "</h1>", "</h2>", "</h3>", "</h4>", "</h5>", "</h6>", "<br", "<hr"]:
            html = html.replace(tag, "\n")

        # Удаляем все остальные теги
        html = re.sub(r"<[^>]+>", "", html)

        # HTML entities
        html = html.replace("&nbsp;", " ")
        html = html.replace("&amp;", "&")
        html = html.replace("&lt;", "<")
        html = html.replace("&gt;", ">")
        html = html.replace("&quot;", '"')
        html = html.replace("&#39;", "'")

        # Убираем множественные пустые строки
        html = re.sub(r"\n{3,}", "\n\n", html)

        # Убираем пробелы в начале/конце строк
        lines = [line.strip() for line in html.split("\n")]
        html = "\n".join(line for line in lines if line)

        return html.strip()


# Singleton
page_fetcher = WebPageFetcher()
