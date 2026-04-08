"""Memory retrieval service with vector search.

Hybrid retrieval: vector similarity + keyword search + recency weighting.
Returns top-k most relevant memory items for a query.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import structlog
from sqlalchemy import select, and_, or_, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.database.session import get_session
from src.database.models import MemoryItem, MemorySummary, MemoryType
from src.memory_services.embedding_service import embedding_service
from src.memory_services.models import MemorySearchResult, MemoryScoreResult

logger = structlog.get_logger()


class RetrievalService:
    """Hybrid memory retrieval with vector search and keyword fallback."""

    def __init__(self) -> None:
        self.top_k = getattr(settings, "memory_retrieval_top_k", 10)
        self.max_context_tokens = settings.max_context_tokens

    async def search(
        self,
        query: str,
        chat_id: int | None = None,
        user_id: int | None = None,
        memory_types: list[MemoryType] | None = None,
        top_k: int | None = None,
        include_inactive: bool = False,
    ) -> list[MemorySearchResult]:
        """Search memory with hybrid approach.

        1. Vector similarity search (if embeddings available)
        2. Keyword search fallback
        3. Recency-weighted scoring
        """
        top_k = top_k or self.top_k

        # Try vector search first
        vector_results = await self._vector_search(
            query, chat_id, user_id, memory_types, top_k * 2, include_inactive
        )

        # Keyword search fallback
        keyword_results = await self._keyword_search(
            query, chat_id, user_id, memory_types, top_k * 2, include_inactive
        )

        # Merge and deduplicate
        merged = self._merge_results(vector_results, keyword_results)

        # Score and rank
        scored = self._score_results(merged, query)

        # Return top-k
        return sorted(scored, key=lambda x: x.score, reverse=True)[:top_k]

    async def search_by_topic(
        self,
        topic: str,
        chat_id: int,
        top_k: int | None = None,
    ) -> list[MemorySearchResult]:
        """Search memory by topic for a specific chat."""
        return await self.search(
            query=topic,
            chat_id=chat_id,
            top_k=top_k,
        )

    async def get_user_memories(
        self,
        user_id: int,
        chat_id: int | None = None,
        top_k: int | None = None,
    ) -> list[MemorySearchResult]:
        """Get most relevant memories for a specific user."""
        return await self.search(
            query="",  # Get all relevant memories
            chat_id=chat_id,
            user_id=user_id,
            top_k=top_k or 20,
        )

    async def get_chat_context(
        self,
        chat_id: int,
        query: str = "",
        top_k: int | None = None,
    ) -> list[MemorySearchResult]:
        """Get relevant context for a chat."""
        return await self.search(
            query=query,
            chat_id=chat_id,
            top_k=top_k,
        )

    async def get_related_summaries(
        self,
        query: str,
        chat_id: int | None = None,
        top_k: int = 3,
    ) -> list[MemorySearchResult]:
        """Search in compacted summaries."""
        if not query:
            return []

        query_embedding = await embedding_service.embed_text(query)

        results: list[MemorySearchResult] = []

        async for session in get_session():
            # Vector similarity with summaries
            if chat_id:
                stmt = text("""
                    SELECT id, chat_id, user_id, content, topics, created_at,
                           embedding_vector <=> :embedding as distance
                    FROM memory_summaries
                    WHERE chat_id = :chat_id
                    ORDER BY distance
                    LIMIT :limit
                """)
            else:
                stmt = text("""
                    SELECT id, chat_id, user_id, content, topics, created_at,
                           embedding_vector <=> :embedding as distance
                    FROM memory_summaries
                    ORDER BY distance
                    LIMIT :limit
                """)

            params = {
                "embedding": str(query_embedding),
                "chat_id": chat_id or 0,
                "limit": top_k,
            }

            result = await session.execute(stmt, params)
            rows = result.fetchall()

            for row in rows:
                results.append(
                    MemorySearchResult(
                        item_id=row.id,
                        type="summary",
                        content=row.content,
                        score=1.0 - min(row.distance, 1.0),
                        chat_id=row.chat_id,
                        user_id=row.user_id,
                        created_at=row.created_at,
                    )
                )

        return results

    async def _vector_search(
        self,
        query: str,
        chat_id: int | None = None,
        user_id: int | None = None,
        memory_types: list[MemoryType] | None = None,
        limit: int = 20,
        include_inactive: bool = False,
    ) -> list[MemorySearchResult]:
        """Search using vector similarity."""
        if not query:
            return []

        query_embedding = await embedding_service.embed_text(query)
        results: list[MemorySearchResult] = []

        async for session in get_session():
            # Build WHERE clause
            conditions = [MemoryItem.type != MemoryType.RAW_MESSAGE]
            if chat_id:
                conditions.append(MemoryItem.chat_id == chat_id)
            if user_id:
                conditions.append(MemoryItem.user_id == user_id)
            if memory_types:
                conditions.append(MemoryItem.type.in_(memory_types))
            if not include_inactive:
                conditions.append(MemoryItem.is_active == True)

            # Use pgvector cosine similarity
            # embedding <=> query_embedding returns distance (lower is better)
            embedding_str = str(query_embedding)

            stmt = text(f"""
                SELECT id, chat_id, user_id, type, content, confidence, relevance,
                       frequency, access_count, created_at, last_used_at,
                       embedding_vector <=> :embedding as distance
                FROM memory_items
                WHERE {" AND ".join(str(cond) for cond in self._sql_conditions(conditions))}
                  AND embedding_vector IS NOT NULL
                ORDER BY distance
                LIMIT :limit
            """)

            params = {"embedding": embedding_str, "limit": limit}
            if chat_id is not None:
                params["chat_id"] = chat_id
            if user_id is not None:
                params["user_id"] = user_id
            result = await session.execute(stmt, params)
            rows = result.fetchall()

            for row in rows:
                results.append(
                    MemorySearchResult(
                        item_id=row.id,
                        type=row.type,
                        content=row.content,
                        score=1.0 - min(row.distance, 1.0),  # Convert distance to similarity
                        chat_id=row.chat_id,
                        user_id=row.user_id,
                        created_at=row.created_at,
                        relevance=row.relevance,
                        frequency=row.frequency,
                    )
                )

        return results

    async def _keyword_search(
        self,
        query: str,
        chat_id: int | None = None,
        user_id: int | None = None,
        memory_types: list[MemoryType] | None = None,
        limit: int = 20,
        include_inactive: bool = False,
    ) -> list[MemorySearchResult]:
        """Fallback keyword search when vector search is not available."""
        if not query:
            # Return most relevant recent memories
            return await self._get_recent_memories(chat_id, user_id, memory_types, limit, include_inactive)

        results: list[MemorySearchResult] = []
        keywords = [w.lower() for w in query.split() if len(w) > 2]

        if not keywords:
            return []

        async for session in get_session():
            conditions = [MemoryItem.type != MemoryType.RAW_MESSAGE]
            if chat_id:
                conditions.append(MemoryItem.chat_id == chat_id)
            if user_id:
                conditions.append(MemoryItem.user_id == user_id)
            if memory_types:
                conditions.append(MemoryItem.type.in_(memory_types))
            if not include_inactive:
                conditions.append(MemoryItem.is_active == True)

            # Keyword matching with ILIKE
            keyword_conditions = []
            for keyword in keywords:
                keyword_conditions.append(MemoryItem.content.ilike(f"%{keyword}%"))
                # Also search in tags
                keyword_conditions.append(
                    text(f"EXISTS (SELECT 1 FROM unnest(tags) t WHERE t ILIKE '%{keyword}%')")
                )

            stmt = (
                select(MemoryItem)
                .where(and_(*conditions))
                .where(or_(*keyword_conditions))
                .order_by(MemoryItem.relevance.desc(), MemoryItem.created_at.desc())
                .limit(limit)
            )

            result = await session.execute(stmt)
            items = result.scalars().all()

            for item in items:
                # Score by keyword match quality
                content_lower = item.content.lower()
                matches = sum(1 for kw in keywords if kw in content_lower)
                keyword_score = matches / len(keywords) if keywords else 0

                results.append(
                    MemorySearchResult(
                        item_id=item.id,
                        type=item.type.value,
                        content=item.content,
                        score=keyword_score,
                        chat_id=item.chat_id,
                        user_id=item.user_id,
                        created_at=item.created_at,
                        relevance=item.relevance,
                        frequency=item.frequency,
                    )
                )

        return results

    async def _get_recent_memories(
        self,
        chat_id: int | None = None,
        user_id: int | None = None,
        memory_types: list[MemoryType] | None = None,
        limit: int = 20,
        include_inactive: bool = False,
    ) -> list[MemorySearchResult]:
        """Get recent memories when no query is provided."""
        results: list[MemorySearchResult] = []

        async for session in get_session():
            conditions = [MemoryItem.type != MemoryType.RAW_MESSAGE]
            if chat_id:
                conditions.append(MemoryItem.chat_id == chat_id)
            if user_id:
                conditions.append(MemoryItem.user_id == user_id)
            if memory_types:
                conditions.append(MemoryItem.type.in_(memory_types))
            if not include_inactive:
                conditions.append(MemoryItem.is_active == True)

            stmt = (
                select(MemoryItem)
                .where(and_(*conditions))
                .order_by(MemoryItem.relevance.desc(), MemoryItem.created_at.desc())
                .limit(limit)
            )

            result = await session.execute(stmt)
            items = result.scalars().all()

            for item in items:
                results.append(
                    MemorySearchResult(
                        item_id=item.id,
                        type=item.type.value,
                        content=item.content,
                        score=item.relevance,
                        chat_id=item.chat_id,
                        user_id=item.user_id,
                        created_at=item.created_at,
                        relevance=item.relevance,
                        frequency=item.frequency,
                    )
                )

        return results

    def _merge_results(
        self,
        vector_results: list[MemorySearchResult],
        keyword_results: list[MemorySearchResult],
    ) -> list[MemorySearchResult]:
        """Merge and deduplicate results."""
        seen_ids: set[int] = set()
        merged: list[MemorySearchResult] = []

        # Prefer vector results
        for result in vector_results:
            if result.item_id not in seen_ids:
                seen_ids.add(result.item_id)
                merged.append(result)

        # Add keyword results not in vector
        for result in keyword_results:
            if result.item_id not in seen_ids:
                seen_ids.add(result.item_id)
                merged.append(result)

        return merged

    def _score_results(
        self,
        results: list[MemorySearchResult],
        query: str,
    ) -> list[MemorySearchResult]:
        """Apply recency and frequency weighting to scores."""
        now = datetime.now(timezone.utc)

        for result in results:
            # Recency score (newer = higher)
            recency_score = self._recency_score(result.created_at, now)

            # Frequency boost (often accessed = more important)
            frequency_score = min(result.frequency / 10.0, 0.3)

            # Relevance from DB
            relevance_score = result.relevance

            # Combined score
            # Base score from search * (1 + recency + frequency + relevance bonuses)
            result.score = result.score * (1.0 + recency_score + frequency_score + relevance_score * 0.5)

        return results

    def _recency_score(self, created_at: datetime | None, now: datetime) -> float:
        """Calculate recency score with exponential decay."""
        if not created_at:
            return 0.0

        # Hours since creation
        hours_old = (now - created_at).total_seconds() / 3600

        # Half-life: 72 hours (3 days)
        half_life = getattr(settings, "memory_decay_half_life", 72)

        # Exponential decay
        score = math.exp(-math.log(2) * hours_old / half_life)

        return score

    def _sql_conditions(self, conditions: list) -> list[str]:
        """Convert SQLAlchemy conditions to SQL strings for text() queries."""
        # For simplicity, we build conditions manually
        sql_conds = []
        for cond in conditions:
            if cond == MemoryItem.type != MemoryType.RAW_MESSAGE:
                sql_conds.append("type != 'raw_message'")
            elif hasattr(cond, "left") and hasattr(cond, "right"):
                # Simple equality conditions
                left = str(cond.left.key) if hasattr(cond.left, "key") else str(cond.left)
                if left == "chat_id":
                    sql_conds.append(f"chat_id = :chat_id")
                elif left == "user_id":
                    sql_conds.append(f"user_id = :user_id")
        return sql_conds if sql_conds else ["TRUE"]

    async def record_access(self, item_id: int) -> None:
        """Record that a memory item was accessed (for scoring)."""
        async for session in get_session():
            stmt = text("""
                UPDATE memory_items
                SET access_count = access_count + 1,
                    last_used_at = NOW(),
                    frequency = frequency + 1
                WHERE id = :item_id
            """)
            await session.execute(stmt, {"item_id": item_id})
            await session.commit()


# Singleton
retrieval_service = RetrievalService()
