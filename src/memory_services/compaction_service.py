"""Context compaction service.

Compacts old conversation segments into summaries to prevent context overflow.
Pipeline: memory flush → summarize → save summary → mark original messages as compacted.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.database.session import get_session
from src.database.models import Message, MemorySummary, MemoryItem, MemoryType
from src.llm_adapter.base import LLMProvider
from src.memory_services.embedding_service import embedding_service
from src.memory_services.extraction_service import extraction_service
from src.memory_services.models import CompactionResult

logger = structlog.get_logger()

SUMMARIZE_PROMPT = """Ты — система сжатия диалогов. Проанализируй переписку и создай краткое содержание.

Создай:
1. Краткое содержание (3-5 предложений) — что происходило, о чём говорили
2. Ключевые темы (список)
3. Важные события (что произошло)
4. Участников (кто был активен)

НЕ выдумывай факты. Только то, что было в диалоге.
Если диалог бессвязный — скажи об этом.

Ответь в формате JSON:
```json
{
  "summary": "краткое содержание",
  "topics": ["тема1", "тема2"],
  "key_events": ["событие1", "событие2"],
  "active_participants": ["участник1", "участник2"]
}
```

Диалог:
{conversation}
"""


class CompactionService:
    """Compact old conversations into summaries to save context space."""

    def __init__(self) -> None:
        self.llm_provider = LLMProvider.get_provider()
        self.compaction_threshold = getattr(settings, "compaction_threshold_tokens", 3000)
        self.messages_per_batch = getattr(settings, "compaction_messages_per_batch", 50)
        self.compact_after_hours = getattr(settings, "compact_after_hours", 24)

    async def compact_chat(
        self,
        chat_id: int,
        max_messages: int | None = None,
    ) -> list[CompactionResult]:
        """Compact old messages for a chat into summaries.

        Args:
            chat_id: Chat to compact
            max_messages: Maximum number of messages to process (None = use default)

        Returns:
            List of compaction results
        """
        max_msgs = max_messages or self.messages_per_batch

        # Get old messages eligible for compaction
        messages = await self._get_compactable_messages(chat_id, max_msgs)

        if not messages:
            logger.debug("No messages to compact", chat_id=chat_id)
            return []

        # Flush memory before compaction (extract any remaining facts)
        await self._flush_memory_before_compaction(chat_id, messages)

        # Group messages into batches
        batches = self._group_messages(messages)

        results = []
        for batch in batches:
            result = await self._compact_batch(chat_id, batch)
            if result:
                results.append(result)

        logger.info(
            "Compaction completed",
            chat_id=chat_id,
            batches_compacted=len(results),
            total_messages=sum(r.original_messages_count for r in results),
        )

        return results

    async def compact_all_chats(self) -> dict[int, list[CompactionResult]]:
        """Run compaction on all chats with old messages."""
        from src.database.models import Chat, ChatType

        async for session in get_session():
            # Get all group chats
            stmt = select(Message.chat_id, func.count(Message.id).label("msg_count")).where(
                Message.created_at < datetime.now(timezone.utc) - timedelta(hours=self.compact_after_hours)
            ).group_by(Message.chat_id)

            result = await session.execute(stmt)
            rows = result.fetchall()

        results: dict[int, list[CompactionResult]] = {}

        for row in rows:
            chat_id = row.chat_id
            chat_results = await self.compact_chat(chat_id)
            if chat_results:
                results[chat_id] = chat_results

        return results

    async def _get_compactable_messages(
        self,
        chat_id: int,
        max_messages: int,
    ) -> list[Message]:
        """Get messages older than compaction threshold."""
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=self.compact_after_hours)

        async for session in get_session():
            # Get messages that are:
            # 1. Older than compaction threshold
            # 2. Not already compacted (no summary exists for them)
            stmt = (
                select(Message)
                .where(
                    and_(
                        Message.chat_id == chat_id,
                        Message.created_at < cutoff_time,
                        # Exclude messages already covered by summaries
                        Message.id.notin_(
                            select(MemorySummary.start_message_id).where(
                                MemorySummary.chat_id == chat_id
                            ).union(
                                select(MemorySummary.end_message_id).where(
                                    MemorySummary.chat_id == chat_id
                                )
                            )
                        ),
                    )
                )
                .order_by(Message.created_at)
                .limit(max_messages)
            )

            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def _flush_memory_before_compaction(
        self,
        chat_id: int,
        messages: list[Message],
    ) -> None:
        """Extract any remaining facts before compacting."""
        if not messages:
            return

        msg_dicts = [
            {
                "user_id": m.user_id,
                "username": None,
                "text": m.text,
                "created_at": m.created_at.isoformat() if m.created_at else "",
            }
            for m in messages
        ]

        try:
            # Extract facts from messages before compacting
            await extraction_service.extract_from_batch(
                chat_id=chat_id,
                messages=msg_dicts,
                batch_start_id=min(m.id for m in messages),
                batch_end_id=max(m.id for m in messages),
            )
        except Exception:
            logger.exception("Memory flush before compaction failed", chat_id=chat_id)

    def _group_messages(
        self,
        messages: list[Message],
    ) -> list[list[Message]]:
        """Group messages into batches for summarization."""
        batches: list[list[Message]] = []
        current_batch: list[Message] = []

        for msg in messages:
            current_batch.append(msg)

            if len(current_batch) >= self.messages_per_batch:
                batches.append(current_batch)
                current_batch = []

        if current_batch:
            batches.append(current_batch)

        return batches

    async def _compact_batch(
        self,
        chat_id: int,
        messages: list[Message],
    ) -> CompactionResult | None:
        """Summarize a batch of messages and save as compacted summary."""
        if not messages:
            return None

        # Format conversation
        conversation = self._format_messages(messages)

        # Generate summary via LLM
        summary_data = await self._generate_summary(conversation)

        if not summary_data:
            logger.warning("Failed to generate summary for batch", chat_id=chat_id)
            return None

        # Calculate estimated token savings
        original_tokens = self._estimate_tokens(conversation)
        summary_tokens = self._estimate_tokens(summary_data["summary"])
        saved_tokens = original_tokens - summary_tokens

        # Save summary to database
        summary_id = await self._save_summary(
            chat_id=chat_id,
            content=summary_data["summary"],
            topics=summary_data.get("topics", []),
            messages=messages,
        )

        # Generate embedding for the summary
        if summary_id:
            embedding = await embedding_service.embed_text(summary_data["summary"])
            await self._update_summary_embedding(summary_id, embedding)

        return CompactionResult(
            summary_id=summary_id,
            original_messages_count=len(messages),
            summary_text=summary_data["summary"],
            saved_tokens_estimate=max(saved_tokens, 0),
        )

    async def _generate_summary(self, conversation: str) -> dict | None:
        """Call LLM to generate summary."""
        prompt = SUMMARIZE_PROMPT.format(conversation=conversation)

        messages = [
            {"role": "system", "content": "Ты — система сжатия диалогов. Отвечай строго в формате JSON."},
            {"role": "user", "content": prompt},
        ]

        try:
            response = await self.llm_provider.generate_response(messages=messages)

            # Parse JSON
            if "```json" in response:
                json_str = response.split("```json")[1].split("```")[0].strip()
            elif "```" in response:
                json_str = response.split("```")[1].split("```")[0].strip()
            else:
                json_str = response

            import json
            data = json.loads(json_str)

            return {
                "summary": data.get("summary", ""),
                "topics": data.get("topics", []),
                "key_events": data.get("key_events", []),
                "participants": data.get("active_participants", []),
            }

        except Exception:
            logger.exception("Summary generation failed")
            return None

    async def _save_summary(
        self,
        chat_id: int,
        content: str,
        topics: list[str],
        messages: list[Message],
    ) -> int | None:
        """Save summary to database."""
        if not messages:
            return None

        async for session in get_session():
            summary = MemorySummary(
                chat_id=chat_id,
                user_id=None,  # Summary covers multiple users
                content=content,
                topics=topics if topics else None,
                start_message_id=min(m.id for m in messages),
                end_message_id=max(m.id for m in messages),
                start_time=min(m.created_at for m in messages),
                end_time=max(m.created_at for m in messages),
                message_count=len(messages),
            )

            session.add(summary)
            await session.commit()
            await session.refresh(summary)

            return summary.id

        return None

    async def _update_summary_embedding(self, summary_id: int, embedding: list[float]) -> None:
        """Update summary with embedding vector."""
        from sqlalchemy import text

        async for session in get_session():
            stmt = text("""
                UPDATE memory_summaries
                SET embedding_vector = :embedding
                WHERE id = :summary_id
            """)
            await session.execute(stmt, {
                "embedding": str(embedding),
                "summary_id": summary_id,
            })
            await session.commit()

    def _format_messages(self, messages: list[Message]) -> str:
        """Format messages into readable text."""
        lines = []
        for msg in messages:
            author = f"user_{msg.user_id}" if msg.user_id else "кто-то"
            lines.append(f"[{author}]: {msg.text}")
        return "\n".join(lines)

    def _estimate_tokens(self, text: str) -> int:
        """Rough token count estimate."""
        # ~4 chars per token for Russian/English
        return len(text) // 4


# Singleton
compaction_service = CompactionService()
