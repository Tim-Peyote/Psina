from abc import ABC, abstractmethod
from dataclasses import dataclass

import structlog

from src.config import settings

logger = structlog.get_logger()


@dataclass
class LLMUsage:
    tokens_prompt: int
    tokens_completion: int
    model: str


class BaseLLMProvider(ABC):
    """Abstract interface for LLM providers."""

    @abstractmethod
    async def generate_response(
        self,
        messages: list[dict],
        chat_id: int = 0,
        user_id: int = 0,
    ) -> str:
        """Generate a response given a list of messages."""
        ...

    @abstractmethod
    async def summarize(self, texts: list[str], max_tokens: int = 500) -> str:
        """Summarize a list of texts."""
        ...

    @abstractmethod
    async def extract_facts(self, text: str) -> list[str]:
        """Extract facts from text."""
        ...

    @abstractmethod
    async def classify(self, text: str, categories: list[str]) -> str:
        """Classify text into one of the given categories."""
        ...


class LLMProvider:
    """Registry and factory for LLM providers."""

    _providers: dict[str, type[BaseLLMProvider]] = {}
    _instance: BaseLLMProvider | None = None

    @classmethod
    def register(cls, name: str, provider_class: type[BaseLLMProvider]) -> None:
        cls._providers[name] = provider_class
        logger.info("Registered LLM provider", name=name)

    @classmethod
    def get_provider(cls) -> BaseLLMProvider:
        """Get or create the active provider instance."""
        if cls._instance is None:
            provider_name = settings.llm_provider
            provider_class = cls._providers.get(provider_name)
            if provider_class is None:
                logger.warning(
                    "Provider not found, using mock",
                    requested=provider_name,
                    available=list(cls._providers.keys()),
                )
                from src.llm_adapter.mock import MockProvider
                provider_class = MockProvider

            cls._instance = provider_class()
            logger.info("Created LLM provider", name=provider_name)

        return cls._instance

    @classmethod
    def get_fallback_provider(cls) -> BaseLLMProvider:
        """Get fallback provider."""
        from src.llm_adapter.mock import MockProvider
        return MockProvider()


def to_openai_messages(
    system_prompt: str | None,
    user_messages: list[dict],
) -> list[dict]:
    """Convert messages to OpenAI format."""
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.extend(user_messages)
    return messages
