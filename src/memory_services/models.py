"""Pydantic models for the memory pipeline."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class MemoryItemType(str, enum.Enum):
    FACT = "fact"
    PREFERENCE = "preference"
    RELATION = "relation"
    TOPIC = "topic"
    EVENT = "event"
    PLAN = "plan"
    JOKE = "joke"
    GROUP_RULE = "group_rule"
    USER_TRAIT = "user_trait"


class ExtractedMemoryItem(BaseModel):
    """A single memory item extracted by LLM."""

    type: MemoryItemType
    content: str
    user_id: int | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    tags: list[str] = Field(default_factory=list)
    ttl_seconds: int | None = None  # None = permanent


class ExtractionResult(BaseModel):
    """Result of batch extraction."""

    items: list[ExtractedMemoryItem]
    topics: list[str] = Field(default_factory=list)
    summary: str = ""
    key_events: list[str] = Field(default_factory=list)


class MemorySearchResult(BaseModel):
    """Result of memory search."""

    item_id: int
    type: str
    content: str
    score: float
    chat_id: int | None = None
    user_id: int | None = None
    created_at: datetime | None = None
    relevance: float = 0.0
    frequency: int = 1


class ContextPack(BaseModel):
    """Assembled context for LLM generation."""

    system_prompt: str
    recent_messages: list[dict[str, Any]] = Field(default_factory=list)
    session_summary: str = ""
    relevant_memories: list[MemorySearchResult] = Field(default_factory=list)
    user_profile_summary: str = ""
    web_context: str = ""
    knowledge_context: str = ""
    total_tokens_estimate: int = 0


class CompactionResult(BaseModel):
    """Result of context compaction."""

    summary_id: int | None = None
    original_messages_count: int
    summary_text: str
    saved_tokens_estimate: int


class MemoryScoreResult(BaseModel):
    """Computed score for a memory item."""

    item_id: int
    score: float
    relevance: float
    recency: float
    frequency: int
    confidence: float
