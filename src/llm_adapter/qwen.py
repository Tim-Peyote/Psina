import httpx
import structlog
from openai import AsyncOpenAI

from src.config import settings
from src.llm_adapter.base import BaseLLMProvider, to_openai_messages
from src.llm_adapter.budget import token_budget

logger = structlog.get_logger()


class QwenProvider(BaseLLMProvider):
    """Qwen (DashScope) provider using OpenAI-compatible API."""

    def __init__(self) -> None:
        self.client = AsyncOpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        self.model = settings.llm_model
        logger.info("QwenProvider initialized", model=self.model)

    async def generate_response(
        self,
        messages: list[dict],
        chat_id: int = 0,
        user_id: int = 0,
    ) -> str:
        # DON'T add generic system prompt — messages already contain
        # the full personality/vibe system prompt from the orchestrator.
        openai_messages = to_openai_messages(None, messages)

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=openai_messages,
                max_tokens=1024,
                temperature=0.7,
            )
            content = response.choices[0].message.content or ""
            prompt_tokens = response.usage.prompt_tokens if response.usage else 0
            completion_tokens = response.usage.completion_tokens if response.usage else 0
            await token_budget.record_usage(prompt_tokens, completion_tokens)
            logger.debug(
                "Qwen response generated",
                tokens=response.usage.total_tokens if response.usage else 0,
            )
            return content
        except httpx.TimeoutException:
            logger.error("Qwen API timeout", model=self.model)
            from src.llm_adapter.mock import MockProvider
            mock = MockProvider()
            return await mock.generate_response(messages, chat_id, user_id)
        except Exception as e:
            logger.error("Qwen API error", error=str(e))
            from src.llm_adapter.mock import MockProvider
            mock = MockProvider()
            return await mock.generate_response(messages, chat_id, user_id)

    async def summarize(self, texts: list[str], max_tokens: int = 500) -> str:
        combined = "\n".join(texts)
        system_prompt = "Сделай краткую сводку следующего текста. Только факты, без воды."
        messages = to_openai_messages(
            system_prompt,
            [{"role": "user", "content": combined}],
        )

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.3,
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            logger.error("Qwen summarize error", error=str(e))
            return "Не удалось создать сводку."

    async def extract_facts(self, text: str) -> list[str]:
        system_prompt = (
            "Извлеки факты из текста. Каждый факт — отдельная строка. "
            "Только конкретные факты о людях, событиях, предпочтениях. "
            "Не включай мнения и вопросы."
        )
        messages = to_openai_messages(
            system_prompt,
            [{"role": "user", "content": text}],
        )

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=512,
                temperature=0.1,
            )
            content = response.choices[0].message.content or ""
            return [line.strip("-• ").strip() for line in content.split("\n") if line.strip()]
        except Exception as e:
            logger.error("Qwen extract_facts error", error=str(e))
            return []

    async def classify(self, text: str, categories: list[str]) -> str:
        cats = ", ".join(categories)
        system_prompt = f"Классифицируй текст в одну из категорий: {cats}. Ответь только названием категории."
        messages = to_openai_messages(
            system_prompt,
            [{"role": "user", "content": text}],
        )

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=32,
                temperature=0.0,
            )
            return (response.choices[0].message.content or categories[0]).strip()
        except Exception as e:
            logger.error("Qwen classify error", error=str(e))
            return categories[0]
