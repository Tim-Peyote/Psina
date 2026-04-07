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
EXTRACTION_PROMPT = """Ты — система памяти умного Telegram-бота. Проанализируй диалог и извлеки полезную информацию.

Извлеки:
1. ФАКТЫ о людях (профессия, хобби, привычки, навыки)
2. ПРЕДПОЧТЕНИЯ (что любят, что ненавидят, вкусы)
3. СВЯЗИ между людьми (друзья, коллеги, отношения)
4. ТЕМЫ разговора (основные темы обсуждения)
5. ВАЖНЫЕ СОБЫТИЯ (планы, достижения, происшествия)
6. ПЛАНЫ (намерения, будущие действия)
7. ГРУППОВЫЕ ПРАВИЛА (нормы чата, мемы, локальные шутки)

ПРАВИЛА:
- Игнорируй шум: приветствия, флуд, бессмысленные сообщения
- Запоминай только то, что может быть полезно в будущем
- Если человек повторяет что-то несколько раз — это важно
- Не выдумывай факты — только то, что явно сказано
- Для каждого элемента укажи user_id если это о конкретном человеке

Ответь СТРОГО в формате JSON:
```json
{
  "items": [
    {
      "type": "fact|preference|relation|topic|event|plan|group_rule|user_trait|joke",
      "content": "краткое описание факта",
      "user_id": 12345 или null,
      "confidence": 0.0-1.0,
      "tags": ["тег1", "тег2"],
      "ttl_seconds": 86400 или null для постоянных
    }
  ],
  "topics": ["основные темы разговора"],
  "summary": "краткое содержание диалога в 2-3 предложениях",
  "key_events": ["важные события из диалога"]
}
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
        prompt = EXTRACTION_PROMPT.format(conversation=conversation)

        messages = [
            {
                "role": "system",
                "content": "Ты — система извлечения фактов из диалогов. Отвечай строго в формате JSON.",
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
        """Filter out noise and deduplicate items."""
        if not items:
            return []

        filtered = []
        seen_contents: set[str] = set()

        for item in items:
            # Skip empty content
            if not item.content or len(item.content.strip()) < 3:
                continue

            # Skip very low confidence
            if item.confidence < 0.3:
                continue

            # Deduplicate by content similarity
            content_key = item.content.lower()[:50]
            if content_key in seen_contents:
                continue
            seen_contents.add(content_key)

            filtered.append(item)

        return filtered

    async def _save_items(
        self,
        items: list[ExtractedMemoryItem],
        chat_id: int,
    ) -> int:
        """Save extracted items to database with embeddings."""
        if not items:
            return 0

        saved_count = 0

        async for session in get_session():
            for item in items:
                # Generate embedding
                embedding = await embedding_service.embed_text(item.content)

                # Map type
                memory_type = _TYPE_MAP.get(item.type, MemoryType.FACT)

                # Create memory item
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
