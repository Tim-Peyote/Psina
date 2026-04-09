"""
Search Provider Base — базовые классы для поисковых провайдеров.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class SearchResult:
    """Результат поиска."""

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
