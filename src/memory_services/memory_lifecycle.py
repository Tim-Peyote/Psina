"""Memory lifecycle management.

Handles:
- Relevance decay over time
- TTL-based cleanup of weak items
- Consolidation of similar items
- Memory limits enforcement (max per user/chat)
- Forgetting outdated information
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select, and_, func, update, delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.database.session import get_session
from src.database.models import MemoryItem, MemoryType, MemorySummary
from src.memory_services.embedding_service import embedding_service

logger = structlog.get_logger()


class MemoryLifecycleManager:
    """Manage the lifecycle of memory items: decay, cleanup, consolidation, limits."""

    def __init__(self) -> None:
        self.decay_half_life_hours = getattr(settings, "memory_decay_half_life", 72)
        self.max_items_per_user = getattr(settings, "max_memory_items_per_user", 100)
        self.max_items_per_chat = getattr(settings, "max_memory_items_per_chat", 500)
        self.weak_item_ttl_hours = getattr(settings, "memory_ttl_weak_items", 168)  # 7 days default
        self.min_confidence_threshold = 0.2

    async def apply_relevance_decay(self) -> dict:
        """Apply relevance decay to all memory items.

        Updates relevance scores based on age and access patterns.
        Returns stats about the operation.
        """
        stats = {"processed": 0, "deactivated": 0}

        async for session in get_session():
            # Get all active memory items
            stmt = select(MemoryItem).where(MemoryItem.is_active == True)
            result = await session.execute(stmt)
            items = list(result.scalars().all())

            now = datetime.now(timezone.utc)
            items_to_deactivate = []

            for item in items:
                # Calculate new relevance with decay
                old_relevance = item.relevance
                new_relevance = self._calculate_decay_relevance(item, now)

                # Update item
                item.relevance = new_relevance
                stats["processed"] += 1

                # Deactivate very low relevance items
                if new_relevance < self.min_confidence_threshold:
                    items_to_deactivate.append(item.id)

            # Deactivate low-relevance items
            if items_to_deactivate:
                stmt = (
                    update(MemoryItem)
                    .where(MemoryItem.id.in_(items_to_deactivate))
                    .values(is_active=False)
                )
                await session.execute(stmt)
                stats["deactivated"] = len(items_to_deactivate)

            await session.commit()

        logger.info("Relevance decay applied", stats=stats)
        return stats

    async def cleanup_expired_items(self) -> dict:
        """Remove memory items that have expired based on TTL."""
        stats = {"ttl_expired": 0, "weak_removed": 0}

        async for session in get_session():
            now = datetime.now(timezone.utc)

            # 1. Remove TTL-expired items
            expired_stmt = text("""
                UPDATE memory_items
                SET is_active = FALSE
                WHERE ttl_seconds IS NOT NULL
                  AND is_active = TRUE
                  AND created_at + (ttl_seconds || ' seconds')::interval < NOW()
            """)
            result = await session.execute(expired_stmt)
            stats["ttl_expired"] = result.rowcount or 0

            # 2. Remove very old weak items (no TTL, but low confidence & old)
            cutoff_date = now - timedelta(hours=self.weak_item_ttl_hours)
            weak_stmt = (
                update(MemoryItem)
                .where(
                    and_(
                        MemoryItem.is_active == True,
                        MemoryItem.confidence < 0.3,
                        MemoryItem.access_count == 0,
                        MemoryItem.created_at < cutoff_date,
                        MemoryItem.ttl_seconds.is_(None),
                    )
                )
                .values(is_active=False)
            )
            result = await session.execute(weak_stmt)
            stats["weak_removed"] = result.rowcount or 0

            await session.commit()

        logger.info("Expired items cleaned up", stats=stats)
        return stats

    async def consolidate_similar_items(self, chat_id: int | None = None) -> dict:
        """Find and consolidate similar memory items.

        Merges items with similar content into single entries.
        """
        stats = {"groups_found": 0, "items_consolidated": 0, "deleted": 0}

        async for session in get_session():
            # Get items for consolidation
            conditions = [
                MemoryItem.is_active == True,
                MemoryItem.type.in_([MemoryType.FACT, MemoryType.PREFERENCE, MemoryType.EVENT]),
            ]
            if chat_id:
                conditions.append(MemoryItem.chat_id == chat_id)

            stmt = select(MemoryItem).where(and_(*conditions)).order_by(MemoryItem.chat_id, MemoryItem.type)
            result = await session.execute(stmt)
            items = list(result.scalars().all())

            if not items:
                return stats

            # Group by chat_id and type
            groups: dict[tuple, list[MemoryItem]] = {}
            for item in items:
                key = (item.chat_id, item.user_id, item.type)
                if key not in groups:
                    groups[key] = []
                groups[key].append(item)

            # Consolidate each group
            for key, group in groups.items():
                if len(group) < 2:
                    continue

                # Find similar items by content overlap
                consolidated_groups = self._find_similar_groups(group)

                for cons_group in consolidated_groups:
                    if len(cons_group) < 2:
                        continue

                    stats["groups_found"] += 1

                    # Keep the item with highest confidence, merge others
                    cons_group.sort(key=lambda x: x.confidence, reverse=True)
                    primary = cons_group[0]
                    others = cons_group[1:]

                    # Merge content and metadata
                    merged_content = await self._merge_items(primary, others)
                    if merged_content:
                        primary.content = merged_content
                        primary.frequency += sum(o.frequency for o in others)
                        primary.access_count += sum(o.access_count for o in others)

                        # Track which items were consolidated
                        primary.consolidated_from_ids = [o.id for o in others]

                        # Deactivate merged items
                        other_ids = [o.id for o in others]
                        delete_stmt = (
                            update(MemoryItem)
                            .where(MemoryItem.id.in_(other_ids))
                            .values(is_active=False)
                        )
                        await session.execute(delete_stmt)

                        stats["items_consolidated"] += len(others)
                        stats["deleted"] += len(others)

            await session.commit()

        logger.info("Similar items consolidated", stats=stats)
        return stats

    async def enforce_memory_limits(self) -> dict:
        """Enforce maximum memory items per user/chat.

        Removes oldest/least relevant items when limits are exceeded.
        """
        stats = {"user_limits_enforced": 0, "chat_limits_enforced": 0}

        async for session in get_session():
            # 1. Check per-user limits
            if self.max_items_per_user > 0:
                user_stmt = text("""
                    SELECT user_id, COUNT(*) as cnt
                    FROM memory_items
                    WHERE user_id IS NOT NULL AND is_active = TRUE
                    GROUP BY user_id
                    HAVING COUNT(*) > :limit
                """)
                result = await session.execute(user_stmt, {"limit": self.max_items_per_user})
                excess_users = result.fetchall()

                for row in excess_users:
                    user_id = row[0]
                    # Remove oldest/least relevant items
                    cleanup_stmt = text("""
                        WITH ranked AS (
                            SELECT id
                            FROM memory_items
                            WHERE user_id = :user_id AND is_active = TRUE
                            ORDER BY relevance ASC, created_at ASC
                            LIMIT (SELECT COUNT(*) - :limit FROM memory_items WHERE user_id = :user_id AND is_active = TRUE)
                        )
                        UPDATE memory_items
                        SET is_active = FALSE
                        WHERE id IN (SELECT id FROM ranked)
                    """)
                    result = await session.execute(cleanup_stmt, {
                        "user_id": user_id,
                        "limit": self.max_items_per_user,
                    })
                    stats["user_limits_enforced"] += result.rowcount or 0

            # 2. Check per-chat limits
            if self.max_items_per_chat > 0:
                chat_stmt = text("""
                    SELECT chat_id, COUNT(*) as cnt
                    FROM memory_items
                    WHERE chat_id IS NOT NULL AND is_active = TRUE
                    GROUP BY chat_id
                    HAVING COUNT(*) > :limit
                """)
                result = await session.execute(chat_stmt, {"limit": self.max_items_per_chat})
                excess_chats = result.fetchall()

                for row in excess_chats:
                    chat_id = row[0]
                    cleanup_stmt = text("""
                        WITH ranked AS (
                            SELECT id
                            FROM memory_items
                            WHERE chat_id = :chat_id AND is_active = TRUE
                            ORDER BY relevance ASC, created_at ASC
                            LIMIT (SELECT COUNT(*) - :limit FROM memory_items WHERE chat_id = :chat_id AND is_active = TRUE)
                        )
                        UPDATE memory_items
                        SET is_active = FALSE
                        WHERE id IN (SELECT id FROM ranked)
                    """)
                    result = await session.execute(cleanup_stmt, {
                        "chat_id": chat_id,
                        "limit": self.max_items_per_chat,
                    })
                    stats["chat_limits_enforced"] += result.rowcount or 0

            await session.commit()

        logger.info("Memory limits enforced", stats=stats)
        return stats

    async def run_full_cleanup(self) -> dict:
        """Run all cleanup operations in sequence."""
        results = {
            "decay": await self.apply_relevance_decay(),
            "cleanup": await self.cleanup_expired_items(),
            "consolidation": await self.consolidate_similar_items(),
            "limits": await self.enforce_memory_limits(),
        }
        return results

    def _calculate_decay_relevance(self, item: MemoryItem, now: datetime) -> float:
        """Calculate new relevance score with time decay and access bonuses."""
        # Time decay
        age_hours = (now - item.created_at.replace(tzinfo=timezone.utc)).total_seconds() / 3600
        time_decay = math.exp(-math.log(2) * age_hours / self.decay_half_life_hours)

        # Access frequency bonus
        access_bonus = min(item.access_count * 0.05, 0.3)  # Cap at 0.3

        # Base relevance from confidence
        base_relevance = item.confidence

        # Weighted combination
        new_relevance = (base_relevance * 0.4 + time_decay * 0.3 + access_bonus + 0.3)

        # Clamp to [0, 1]
        return max(0.0, min(1.0, new_relevance))

    def _find_similar_groups(self, items: list[MemoryItem]) -> list[list[MemoryItem]]:
        """Group items by content similarity using simple heuristics."""
        groups: list[list[MemoryItem]] = []
        used: set[int] = set()

        for i, item1 in enumerate(items):
            if i in used:
                continue

            current_group = [item1]
            used.add(i)

            for j, item2 in enumerate(items[i + 1:], start=i + 1):
                if j in used:
                    continue

                if self._items_similar(item1, item2):
                    current_group.append(item2)
                    used.add(j)

            groups.append(current_group)

        return groups

    def _items_similar(self, item1: MemoryItem, item2: MemoryItem) -> bool:
        """Check if two items are similar enough to consolidate."""
        content1 = item1.content.lower()
        content2 = item2.content.lower()

        # Simple word overlap
        words1 = set(content1.split())
        words2 = set(content2.split())

        if not words1 or not words2:
            return False

        overlap = len(words1 & words2)
        union = len(words1 | words2)

        # Jaccard similarity
        similarity = overlap / union if union > 0 else 0

        return similarity > 0.5  # 50% overlap threshold

    async def _merge_items(self, primary: MemoryItem, others: list[MemoryItem]) -> str | None:
        """Merge multiple similar items into one content string."""
        if not others:
            return None

        # Combine content with deduplication
        all_contents = [primary.content]
        for other in others:
            # Only add if not already contained
            if other.content.lower() not in primary.content.lower():
                all_contents.append(other.content)

        # Join with separator
        if len(all_contents) > 1:
            return "; ".join(all_contents)

        return None


# Singleton
memory_lifecycle = MemoryLifecycleManager()
