from fastapi import APIRouter
from pydantic import BaseModel

from src.config import settings

router = APIRouter()


class SettingsResponse(BaseModel):
    bot_mode: str
    bot_name: str
    llm_provider: str
    llm_model: str
    llm_fallback_provider: str
    daily_token_budget: int
    max_context_tokens: int
    proactive_cooldown_seconds: int
    quiet_hours_start: int
    quiet_hours_end: int


@router.get("/", response_model=SettingsResponse)
async def get_settings() -> SettingsResponse:
    return SettingsResponse(
        bot_mode=settings.bot_mode,
        bot_name=settings.bot_name,
        llm_provider=settings.llm_provider,
        llm_model=settings.llm_model,
        llm_fallback_provider=settings.llm_fallback_provider,
        daily_token_budget=settings.daily_token_budget,
        max_context_tokens=settings.max_context_tokens,
        proactive_cooldown_seconds=settings.proactive_cooldown_seconds,
        quiet_hours_start=settings.quiet_hours_start,
        quiet_hours_end=settings.quiet_hours_end,
    )


class UpdateModelRequest(BaseModel):
    provider: str
    model: str


@router.post("/model")
async def update_model(request: UpdateModelRequest) -> dict:
    """Update the active LLM model (runtime only, not persisted to config)."""
    settings.llm_provider = request.provider
    settings.llm_model = request.model
    # Reset provider instance to pick up new config
    from src.llm_adapter.base import LLMProvider
    LLMProvider._instance = None
    return {"status": "updated", "provider": request.provider, "model": request.model}
