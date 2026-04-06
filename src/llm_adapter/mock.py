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
        return f"[Mock] Я получил твоё сообщение: «{last_user[:100]}». Сейчас я в режиме разработки."

    async def summarize(self, texts: list[str], max_tokens: int = 500) -> str:
        logger.debug("MockProvider summarize", count=len(texts))
        return f"[Mock] Сводка из {len(texts)} сообщений. (Mock режим)"

    async def extract_facts(self, text: str) -> list[str]:
        logger.debug("MockProvider extract_facts")
        return []

    async def classify(self, text: str, categories: list[str]) -> str:
        logger.debug("MockProvider classify")
        return categories[0] if categories else "unknown"
