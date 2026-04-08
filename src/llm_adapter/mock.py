import structlog

from src.llm_adapter.base import BaseLLMProvider

logger = structlog.get_logger()


class MockProvider(BaseLLMProvider):
    """Mock provider for development and testing."""

    async def generate_response(
        self,
        messages: list[dict],
        chat_id: int = 0,
        user_id: int = 0,
    ) -> str:
        logger.debug("MockProvider generate_response")
        last_user = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                last_user = msg.get("content", "")
                break
        # Return empty string — bot stays silent rather than sending mock garbage
        return ""

    async def summarize(self, texts: list[str], max_tokens: int = 500) -> str:
        logger.debug("MockProvider summarize", count=len(texts))
        return "Не могу сейчас обработать."

    async def extract_facts(self, text: str) -> list[str]:
        logger.debug("MockProvider extract_facts")
        return []

    async def classify(self, text: str, categories: list[str]) -> str:
        logger.debug("MockProvider classify")
        return categories[0] if categories else "unknown"
