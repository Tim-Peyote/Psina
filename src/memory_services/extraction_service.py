"""Memory extraction service.

Extracts structured facts from conversation batches using LLM.
Pipeline: batch messages → LLM extraction → filtering → save to memory_items.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import structlog
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.database.session import get_session
from src.database.models import (
    MemoryItem,
    MemoryType,
    Message,
    MemoryExtractionBatch,
)
from src.llm_adapter.base import LLMProvider
from src.memory_services.embedding_service import embedding_service
from src.memory_services.models import (
    ExtractedMemoryItem,
    ExtractionResult,
    MemoryItemType,
)

logger = structlog.get_logger()

# Extraction prompt for LLM
EXTRACTION_PROMPT = """Ты — система долгосрочной памяти умного Telegram-бота. Твоя задача — извлечь ТОЛЬКО самую ценную и долговечную информацию из диалога.

УРОВНИ ВАЖНОСТИ (выбирай строго):

🔴 ПОСТОЯННЫЕ ФАКТЫ (ttl=null, confidence >= 0.8):
Сохраняй ТОЛЬКО если информация явно подтверждена и устойчива:
- Профессия, место работы, должность
- Город / страна проживания
- Семья (дети, партнёр, родители)
- Устойчивое хобби (упоминается как постоянное занятие)
- Язык, специализация, ключевой навык
- Важная черта характера (подтверждённая)

🟡 ПОЛУПОСТОЯННЫЕ (ttl=2592000, confidence >= 0.65):
- Текущий значимый проект или цель
- Сильное выраженное предпочтение/антипатия
- Значимые отношения между участниками чата

🟢 ВРЕМЕННЫЕ (ttl=604800, confidence >= 0.6):
- Конкретные ближайшие планы (с датой или сроком)
- Актуальная проблема которую решают прямо сейчас
- Значимое недавнее событие

СТРОГИЕ ПРАВИЛА:
- MAX 3 факта на весь диалог. Лучше меньше, но точнее.
- НЕ извлекай: приветствия, "ок", "да", "нет", флуд, риторические фразы
- НЕ извлекай: факты об игровых персонажах (RPG, DnD контекст)
- НЕ выдумывай — только то, что явно и конкретно сказано
- Если уверенность < 0.6 — лучше не сохранять
- Для каждого факта укажи user_id если это о конкретном человеке

Ответь СТРОГО в формате JSON (без пояснений, только JSON):
```json
{{
  "items": [
    {{
      "type": "fact|preference|relation|event|plan|group_rule",
      "content": "чёткое краткое описание (1 предложение)",
      "user_id": 12345,
      "confidence": 0.85,
      "tags": ["профессия"],
      "ttl_seconds": null
    }}
  ],
  "topics": ["главные темы (max 3)"],
  "summary": "суть диалога в 1-2 предложениях",
  "key_events": ["только реально важные события"]
}}
```

Диалог:
{conversation}
"""

# Map extraction types to MemoryType
_TYPE_MAP = {
    "fact": MemoryType.FACT,
    "preference": MemoryType.PREFERENCE,
    "relation": MemoryType.RELATIONSHIP,
    "topic": MemoryType.FACT,
    "event": MemoryType.EVENT,
    "plan": MemoryType.FACT,
    "group_rule": MemoryType.GROUP_RULE,
    "user_trait": MemoryType.FACT,
    "joke": MemoryType.FACT,
}


class ExtractionService:
    """Extract structured memory from conversation batches."""

    def __init__(self) -> None:
        self.llm_provider = LLMProvider.get_provider()

    async def extract_from_batch(
        self,
        chat_id: int,
        messages: list[dict],
        batch_start_id: int | None = None,
        batch_end_id: int | None = None,
    ) -> ExtractionResult:
        """Extract memory items from a batch of messages.

        Args:
            chat_id: Chat identifier
            messages: List of message dicts with keys: user_id, username, text, created_at
            batch_start_id: First message ID in batch (for tracking)
            batch_end_id: Last message ID in batch (for tracking)

        Returns:
            ExtractionResult with extracted items and summary
        """
        if not messages:
            return ExtractionResult(items=[], topics=[], summary="", key_events=[])

        # Track batch processing
        batch_record = await self._create_batch_record(
            chat_id,
            batch_start_id or 0,
            batch_end_id or 0,
            len(messages),
        )

        try:
            # Format conversation for LLM
            conversation_text = self._format_conversation(messages)

            # Call LLM for extraction
            result = await self._llm_extract(conversation_text)

            # Filter and deduplicate
            filtered_items = self._filter_items(result.items, chat_id)

            # Save to database
            saved_count = await self._save_items(filtered_items, chat_id)

            # Update batch record
            await self._complete_batch(batch_record, saved_count)

            logger.info(
                "Extraction completed",
                chat_id=chat_id,
                messages_count=len(messages),
                items_extracted=saved_count,
            )

            return ExtractionResult(
                items=filtered_items,
                topics=result.topics,
                summary=result.summary,
                key_events=result.key_events,
            )

        except Exception as e:
            await self._fail_batch(batch_record, str(e))
            logger.exception("Extraction failed", chat_id=chat_id)
            raise

    async def extract_from_message_range(
        self,
        chat_id: int,
        start_message_id: int,
        end_message_id: int,
    ) -> ExtractionResult:
        """Extract memory items from a range of message IDs in the database."""
        async for session in get_session():
            stmt = (
                select(Message)
                .where(
                    and_(
                        Message.chat_id == chat_id,
                        Message.id >= start_message_id,
                        Message.id <= end_message_id,
                    )
                )
                .order_by(Message.id)
            )
            result = await session.execute(stmt)
            messages = list(result.scalars().all())

        if not messages:
            return ExtractionResult(items=[], topics=[], summary="", key_events=[])

        msg_dicts = [
            {
                "user_id": m.user_id,
                "username": getattr(m, "username", None),
                "text": m.text,
                "created_at": m.created_at.isoformat() if m.created_at else "",
            }
            for m in messages
        ]

        return await self.extract_from_batch(
            chat_id,
            msg_dicts,
            batch_start_id=start_message_id,
            batch_end_id=end_message_id,
        )

    async def _llm_extract(self, conversation: str) -> ExtractionResult:
        """Call LLM to extract structured memory."""
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%d.%m.%Y")
        prompt = EXTRACTION_PROMPT.format(conversation=conversation)

        messages = [
            {
                "role": "system",
                "content": f"Сегодня {today}. Ты — система извлечения фактов из диалогов. Отвечай строго в формате JSON.",
            },
            {"role": "user", "content": prompt},
        ]

        response = await self.llm_provider.generate_response(messages=messages)

        # Parse JSON from response
        try:
            # Try to extract JSON from code block
            if "```json" in response:
                json_str = response.split("```json")[1].split("```")[0].strip()
            elif "```" in response:
                json_str = response.split("```")[1].split("```")[0].strip()
            else:
                json_str = response

            data = json.loads(json_str)

            items = []
            for item_data in data.get("items", []):
                try:
                    item_type = MemoryItemType(item_data.get("type", "fact"))
                    items.append(
                        ExtractedMemoryItem(
                            type=item_type,
                            content=item_data.get("content", ""),
                            user_id=item_data.get("user_id"),
                            confidence=item_data.get("confidence", 0.5),
                            tags=item_data.get("tags", []),
                            ttl_seconds=item_data.get("ttl_seconds"),
                        )
                    )
                except (ValueError, TypeError):
                    continue  # Skip invalid items

            return ExtractionResult(
                items=items,
                topics=data.get("topics", []),
                summary=data.get("summary", ""),
                key_events=data.get("key_events", []),
            )

        except (json.JSONDecodeError, KeyError, IndexError) as e:
            logger.warning("Failed to parse LLM extraction JSON", error=str(e))
            return ExtractionResult(items=[], topics=[], summary="", key_events=[])

    def _format_conversation(self, messages: list[dict]) -> str:
        """Format messages into readable conversation for LLM."""
        lines = []
        for msg in messages:
            author = msg.get("username") or msg.get("first_name") or f"user_{msg.get('user_id', '?')}"
            text = msg.get("text", "")
            lines.append(f"[{author}]: {text}")
        return "\n".join(lines)

    def _filter_items(
        self,
        items: list[ExtractedMemoryItem],
        chat_id: int,
    ) -> list[ExtractedMemoryItem]:
        """Filter out noise and deduplicate items within the batch."""
        if not items:
            return []

        filtered = []
        seen_contents: set[str] = set()

        for item in items:
            # Skip empty content
            if not item.content or len(item.content.strip()) < 10:
                continue

            # Raise confidence threshold — quality over quantity
            if item.confidence < 0.6:
                continue

            # Deduplicate within batch by normalized content
            content_key = item.content.lower().strip()[:80]
            if content_key in seen_contents:
                continue
            seen_contents.add(content_key)

            filtered.append(item)

        # Hard cap: max 3 facts per batch (quality over quantity)
        return filtered[:3]

    def _is_duplicate_content(self, content: str, existing_contents: list[str]) -> bool:
        """Check if content is semantically similar to existing items."""
        norm = content.lower().strip()
        norm_key = norm[:100]
        for existing in existing_contents:
            existing_norm = existing.lower().strip()[:100]
            # Overlap check: if 70%+ of characters match at the start
            min_len = min(len(norm_key), len(existing_norm))
            if min_len > 20:
                common = sum(a == b for a, b in zip(norm_key, existing_norm))
                if common / min_len >= 0.7:
                    return True
        return False

    async def _save_items(
        self,
        items: list[ExtractedMemoryItem],
        chat_id: int,
    ) -> int:
        """Save extracted items to database, deduplicating against existing items."""
        if not items:
            return 0

        saved_count = 0

        async for session in get_session():
            # Load recent existing items to check for duplicates
            stmt = (
                select(MemoryItem)
                .where(
                    and_(
                        MemoryItem.chat_id == chat_id,
                        MemoryItem.is_active == True,
                    )
                )
                .order_by(MemoryItem.created_at.desc())
                .limit(50)
            )
            result = await session.execute(stmt)
            existing_items = list(result.scalars().all())
            existing_contents = [m.content for m in existing_items]

            for item in items:
                # Check for duplicates in DB
                if self._is_duplicate_content(item.content, existing_contents):
                    # Update frequency on the most similar existing item instead
                    norm = item.content.lower().strip()[:100]
                    for existing in existing_items:
                        existing_norm = existing.content.lower().strip()[:100]
                        min_len = min(len(norm), len(existing_norm))
                        if min_len > 20:
                            common = sum(a == b for a, b in zip(norm, existing_norm))
                            if common / min_len >= 0.7:
                                existing.frequency = (existing.frequency or 1) + 1
                                existing.confidence = min(1.0, max(existing.confidence, item.confidence))
                                session.add(existing)
                                logger.debug("Duplicate detected, incrementing frequency", content=item.content[:60])
                                break
                    continue

                # Generate embedding
                embedding = await embedding_service.embed_text(item.content)

                # Map type
                memory_type = _TYPE_MAP.get(item.type, MemoryType.FACT)

                memory_item = MemoryItem(
                    chat_id=chat_id,
                    user_id=item.user_id,
                    type=memory_type,
                    content=item.content,
                    embedding_vector=embedding,
                    confidence=item.confidence,
                    relevance=1.0,
                    frequency=1,
                    ttl_seconds=item.ttl_seconds,
                    tags=item.tags if item.tags else None,
                    source="extraction",
                )

                session.add(memory_item)
                existing_contents.append(item.content)
                saved_count += 1

            await session.commit()

        return saved_count

    async def _create_batch_record(
        self,
        chat_id: int,
        start_id: int,
        end_id: int,
        message_count: int,
    ) -> MemoryExtractionBatch:
        """Create a batch tracking record."""
        batch = MemoryExtractionBatch(
            chat_id=chat_id,
            start_message_id=start_id,
            end_message_id=end_id,
            message_count=message_count,
            status="processing",
        )

        async for session in get_session():
            session.add(batch)
            await session.commit()
            await session.refresh(batch)

        return batch

    async def _complete_batch(self, batch: MemoryExtractionBatch, items_count: int) -> None:
        """Mark batch as completed."""
        async for session in get_session():
            batch.status = "completed"
            batch.items_extracted = items_count
            batch.processed_at = datetime.now(timezone.utc)
            await session.commit()

    async def _fail_batch(self, batch: MemoryExtractionBatch, error: str) -> None:
        """Mark batch as failed."""
        async for session in get_session():
            batch.status = "failed"
            batch.error_message = error
            batch.processed_at = datetime.now(timezone.utc)
            await session.commit()


# Singleton
extraction_service = ExtractionService()
