from src.llm_adapter.base import LLMProvider
from src.llm_adapter.qwen import QwenProvider
from src.llm_adapter.openrouter import OpenRouterProvider
from src.llm_adapter.mock import MockProvider

# Register providers
LLMProvider.register("qwen", QwenProvider)
LLMProvider.register("openrouter", OpenRouterProvider)
LLMProvider.register("mock", MockProvider)

# Initialize provider at import time
_ = LLMProvider.get_provider()
