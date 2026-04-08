"""Celery tasks for memory operations.

Periodic tasks:
- extract_memory_batch: Run extraction on unprocessed message batches
- compact_old_sessions: Compact old conversation sessions
- cleanup_expired_memory: Run full memory lifecycle cleanup
- rebuild_embeddings: Rebuild embeddings for items without vectors
"""

from __future__ import annotations

import asyncio

import structlog
from celery import shared_task
from sqlalchemy import select, and_

from src.database.session import get_session
from src.database.models import Message, MemoryExtractionBatch
from src.memory_services.extraction_service import extraction_service
from src.memory_services.compaction_service import compaction_service
from src.memory_services.memory_lifecycle import memory_lifecycle

logger = structlog.get_logger()


def _run_async(coro):
    """Run async code in Celery with a fresh event loop."""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@shared_task(name="src.workers.memory_tasks.extract_memory_batch")
def extract_memory_batch() -> None:
    """Extract memory from unprocessed message batches."""

    async def _run() -> None:
        from src.database.models import Chat, ChatType

        async for session in get_session():
            # Get all active group chats
            stmt = select(Chat).where(
                Chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP])
            )
            result = await session.execute(stmt)
            chats = list(result.scalars().all())

        for chat in chats:
            try:
                # Find unprocessed message ranges
                # Get last processed message ID
                async for session in get_session():
                    stmt = (
                        select(MemoryExtractionBatch)
                        .where(MemoryExtractionBatch.chat_id == chat.id)
                        .where(MemoryExtractionBatch.status.in_(["completed", "failed"]))
                        .order_by(MemoryExtractionBatch.end_message_id.desc())
                        .limit(1)
                    )
                    result = await session.execute(stmt)
                    last_batch = result.scalar_one_or_none()

                    start_id = last_batch.end_message_id + 1 if last_batch else 0

                    # Count new messages
                    stmt = (
                        select(Message)
                        .where(
                            and_(
                                Message.chat_id == chat.id,
                                Message.id >= start_id,
                            )
                        )
                        .order_by(Message.id)
                    )
                    result = await session.execute(stmt)
                    messages = list(result.scalars().all())

                    # Process if we have enough messages
                    if len(messages) >= 20:
                        await extraction_service.extract_from_message_range(
                            chat_id=chat.id,
                            start_message_id=messages[0].id,
                            end_message_id=messages[-1].id,
                        )
                        logger.info(
                            "Memory extraction completed",
                            chat_id=chat.id,
                            messages_count=len(messages),
                        )

            except Exception:
                logger.exception("Failed to extract memory batch", chat_id=chat.id)

    _run_async(_run())


@shared_task(name="src.workers.memory_tasks.compact_old_sessions")
def compact_old_sessions() -> None:
    """Compact old conversation sessions to save context space."""

    async def _run() -> None:
        try:
            results = await compaction_service.compact_all_chats()
            logger.info(
                "Session compaction completed",
                chats_compacted=len(results),
            )
        except Exception:
            logger.exception("Failed to compact old sessions")

    _run_async(_run())


@shared_task(name="src.workers.memory_tasks.cleanup_expired_memory")
def cleanup_expired_memory() -> None:
    """Run full memory lifecycle cleanup."""

    async def _run() -> None:
        try:
            results = await memory_lifecycle.run_full_cleanup()
            logger.info(
                "Memory cleanup completed",
                stats=results,
            )
        except Exception:
            logger.exception("Failed to cleanup expired memory")

    _run_async(_run())


@shared_task(name="src.workers.memory_tasks.rebuild_embeddings")
def rebuild_embeddings() -> None:
    """Rebuild embeddings for memory items without vectors."""

    async def _run() -> None:
        from src.database.models import MemoryItem
        from src.memory_services.embedding_service import embedding_service
        from sqlalchemy import text

        async for session in get_session():
            # Find items without embeddings
            stmt = text("""
                SELECT id, content
                FROM memory_items
                WHERE embedding_vector IS NULL
                  AND type != 'raw_message'
                LIMIT 1000
            """)
            result = await session.execute(stmt)
            items = result.fetchall()

            if not items:
                logger.info("No items need embedding rebuild")
                return

            logger.info("Rebuilding embeddings", items_count=len(items))

            for item in items:
                try:
                    embedding = await embedding_service.embed_text(item.content)

                    update_stmt = text("""
                        UPDATE memory_items
                        SET embedding_vector = :embedding
                        WHERE id = :item_id
                    """)
                    await session.execute(update_stmt, {
                        "embedding": str(embedding),
                        "item_id": item.id,
                    })
                    await session.commit()

                except Exception:
                    logger.exception("Failed to rebuild embedding", item_id=item.id)

    _run_async(_run())
