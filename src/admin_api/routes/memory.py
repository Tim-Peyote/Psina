from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from src.database.session import get_session
from src.database.models import MemoryItem, MemoryType, MemorySummary, MemoryExtractionBatch
from sqlalchemy import select, and_, func, text

router = APIRouter(tags=["memory"])


class MemoryItemResponse(BaseModel):
    id: int
    chat_id: int | None
    user_id: int | None
    type: str
    content: str
    confidence: float
    relevance: float
    frequency: int
    access_count: int
    is_active: bool
    source: str
    tags: list[str] | None
    created_at: str | None

    class Config:
        from_attributes = True


class MemoryListResponse(BaseModel):
    items: list[MemoryItemResponse]
    total: int


@router.get("/", response_model=MemoryListResponse, summary="List memory items")
async def list_memories(
    chat_id: int | None = Query(None),
    user_id: int | None = Query(None),
    memory_type: str | None = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
) -> MemoryListResponse:
    async for session in get_session():
        stmt = select(MemoryItem)
        conditions = []
        if chat_id is not None:
            conditions.append(MemoryItem.chat_id == chat_id)
        if user_id is not None:
            conditions.append(MemoryItem.user_id == user_id)
        if memory_type is not None:
            try:
                conditions.append(MemoryItem.type == MemoryType(memory_type))
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid memory type: {memory_type}")

        if conditions:
            stmt = stmt.where(and_(*conditions))

        # Count
        count_stmt = select(MemoryItem)
        if conditions:
            count_stmt = count_stmt.where(and_(*conditions))
        from sqlalchemy import func
        count_result = await session.execute(
            select(func.count()).select_from(count_stmt.subquery())
        )
        total = count_result.scalar() or 0

        stmt = stmt.order_by(MemoryItem.created_at.desc()).offset(offset).limit(limit)
        result = await session.execute(stmt)
        items = list(result.scalars().all())

        return MemoryListResponse(
            items=[
                MemoryItemResponse(
                    id=item.id,
                    chat_id=item.chat_id,
                    user_id=item.user_id,
                    type=item.type.value,
                    content=item.content,
                    confidence=item.confidence,
                    relevance=item.relevance,
                    frequency=item.frequency,
                    access_count=item.access_count,
                    is_active=item.is_active,
                    source=item.source,
                    tags=item.tags,
                    created_at=item.created_at.isoformat() if item.created_at else None,
                )
                for item in items
            ],
            total=total,
        )


@router.delete("/{item_id}", summary="Delete a memory item")
async def delete_memory(item_id: int) -> dict:
    async for session in get_session():
        stmt = select(MemoryItem).where(MemoryItem.id == item_id)
        result = await session.execute(stmt)
        item = result.scalar_one_or_none()
        if not item:
            raise HTTPException(status_code=404, detail="Memory item not found")
        await session.delete(item)
        await session.commit()
        return {"status": "deleted", "id": item_id}


@router.post("/clear", summary="Clear memories by chat or user")
async def clear_memories(
    chat_id: int | None = Query(None),
    user_id: int | None = Query(None),
) -> dict:
    async for session in get_session():
        stmt = select(MemoryItem)
        if chat_id is not None:
            stmt = stmt.where(MemoryItem.chat_id == chat_id)
        if user_id is not None:
            stmt = stmt.where(MemoryItem.user_id == user_id)

        result = await session.execute(stmt)
        items = list(result.scalars().all())
        count = len(items)

        for item in items:
            await session.delete(item)
        await session.commit()

        return {"status": "cleared", "deleted_count": count}


# ========== NEW MEMORY SYSTEM ENDPOINTS ==========


class MemorySearchRequest(BaseModel):
    query: str
    chat_id: int | None = None
    user_id: int | None = None
    top_k: int = 10


class MemorySearchResult(BaseModel):
    item_id: int
    type: str
    content: str
    score: float
    chat_id: int | None = None
    user_id: int | None = None
    relevance: float = 0.0
    frequency: int = 1


@router.post("/search", response_model=list[MemorySearchResult], summary="Search memory (hybrid vector + keyword)")
async def search_memory(request: MemorySearchRequest) -> list[MemorySearchResult]:
    """Search memory with hybrid vector + keyword approach."""
    from src.memory_services.retrieval_service import retrieval_service

    results = await retrieval_service.search(
        query=request.query,
        chat_id=request.chat_id,
        user_id=request.user_id,
        top_k=request.top_k,
    )

    return [
        MemorySearchResult(
            item_id=r.item_id,
            type=r.type,
            content=r.content,
            score=r.score,
            chat_id=r.chat_id,
            user_id=r.user_id,
            relevance=r.relevance,
            frequency=r.frequency,
        )
        for r in results
    ]


class MemoryStatsResponse(BaseModel):
    total_items: int
    active_items: int
    inactive_items: int
    items_by_type: dict[str, int]
    items_by_chat: dict[str, int]
    avg_relevance: float
    avg_confidence: float


@router.get("/stats", response_model=MemoryStatsResponse, summary="Memory system statistics")
async def memory_stats() -> MemoryStatsResponse:
    """Get memory system statistics."""
    async for session in get_session():
        # Total counts
        total_stmt = select(func.count(MemoryItem.id))
        result = await session.execute(total_stmt)
        total = result.scalar() or 0

        active_stmt = select(func.count(MemoryItem.id)).where(MemoryItem.is_active == True)
        result = await session.execute(active_stmt)
        active = result.scalar() or 0

        # By type
        type_stmt = select(MemoryItem.type, func.count(MemoryItem.id)).group_by(MemoryItem.type)
        result = await session.execute(type_stmt)
        by_type = {t.value: c for t, c in result.fetchall()}

        # By chat (top 10)
        chat_stmt = select(MemoryItem.chat_id, func.count(MemoryItem.id)).where(
            MemoryItem.chat_id.isnot(None)
        ).group_by(MemoryItem.chat_id).order_by(func.count(MemoryItem.id).desc()).limit(10)
        result = await session.execute(chat_stmt)
        by_chat = {str(cid): cnt for cid, cnt in result.fetchall() if cid}

        # Averages
        avg_stmt = select(
            func.avg(MemoryItem.relevance),
            func.avg(MemoryItem.confidence),
        )
        result = await session.execute(avg_stmt)
        row = result.first()
        avg_relevance = float(row[0]) if row and row[0] else 0.0
        avg_confidence = float(row[1]) if row and row[1] else 0.0

        return MemoryStatsResponse(
            total_items=total,
            active_items=active,
            inactive_items=total - active,
            items_by_type=by_type,
            items_by_chat=by_chat,
            avg_relevance=avg_relevance,
            avg_confidence=avg_confidence,
        )


class MemoryLifecycleResult(BaseModel):
    status: str
    details: dict


@router.post("/lifecycle/decay", response_model=MemoryLifecycleResult, summary="Trigger relevance decay")
async def trigger_decay() -> MemoryLifecycleResult:
    """Manually trigger relevance decay."""
    from src.memory_services.memory_lifecycle import memory_lifecycle

    result = await memory_lifecycle.apply_relevance_decay()
    return MemoryLifecycleResult(status="success", details=result)


@router.post("/lifecycle/cleanup", response_model=MemoryLifecycleResult, summary="Trigger expired items cleanup")
async def trigger_cleanup() -> MemoryLifecycleResult:
    """Manually trigger expired items cleanup."""
    from src.memory_services.memory_lifecycle import memory_lifecycle

    result = await memory_lifecycle.cleanup_expired_items()
    return MemoryLifecycleResult(status="success", details=result)


@router.post("/lifecycle/consolidate", response_model=MemoryLifecycleResult, summary="Trigger similar items consolidation")
async def trigger_consolidation() -> MemoryLifecycleResult:
    """Manually trigger similar items consolidation."""
    from src.memory_services.memory_lifecycle import memory_lifecycle

    result = await memory_lifecycle.consolidate_similar_items()
    return MemoryLifecycleResult(status="success", details=result)


@router.post("/lifecycle/full", response_model=MemoryLifecycleResult, summary="Run full memory lifecycle cleanup")
async def trigger_full_lifecycle() -> MemoryLifecycleResult:
    """Run full memory lifecycle cleanup."""
    from src.memory_services.memory_lifecycle import memory_lifecycle

    result = await memory_lifecycle.run_full_cleanup()
    return MemoryLifecycleResult(status="success", details=result)


class MemorySummariesResponse(BaseModel):
    summaries: list[dict]
    total: int


@router.get("/summaries", response_model=MemorySummariesResponse, summary="List memory summaries")
async def list_summaries(
    chat_id: int | None = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
) -> MemorySummariesResponse:
    """List memory summaries."""
    async for session in get_session():
        stmt = select(MemorySummary)
        conditions = []
        if chat_id is not None:
            conditions.append(MemorySummary.chat_id == chat_id)

        if conditions:
            stmt = stmt.where(and_(*conditions))

        # Count
        count_stmt = select(func.count(MemorySummary.id))
        if conditions:
            count_stmt = count_stmt.where(and_(*conditions))
        result = await session.execute(count_stmt)
        total = result.scalar() or 0

        stmt = stmt.order_by(MemorySummary.created_at.desc()).offset(offset).limit(limit)
        result = await session.execute(stmt)
        summaries = list(result.scalars().all())

        return MemorySummariesResponse(
            summaries=[
                {
                    "id": s.id,
                    "chat_id": s.chat_id,
                    "content": s.content[:200] + "..." if len(s.content) > 200 else s.content,
                    "topics": s.topics or [],
                    "message_count": s.message_count,
                    "created_at": s.created_at.isoformat() if s.created_at else None,
                }
                for s in summaries
            ],
            total=total,
        )


class ExtractionBatchResponse(BaseModel):
    id: int
    chat_id: int
    message_count: int
    items_extracted: int
    status: str
    created_at: str | None
    processed_at: str | None


@router.get("/extraction-batches", response_model=list[ExtractionBatchResponse], summary="List extraction batches")
async def list_extraction_batches(
    chat_id: int | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(50, le=200),
) -> list[ExtractionBatchResponse]:
    """List extraction batches."""
    async for session in get_session():
        stmt = select(MemoryExtractionBatch)
        if chat_id is not None:
            stmt = stmt.where(MemoryExtractionBatch.chat_id == chat_id)
        if status is not None:
            stmt = stmt.where(MemoryExtractionBatch.status == status)

        stmt = stmt.order_by(MemoryExtractionBatch.created_at.desc()).limit(limit)
        result = await session.execute(stmt)
        batches = list(result.scalars().all())

        return [
            ExtractionBatchResponse(
                id=b.id,
                chat_id=b.chat_id,
                message_count=b.message_count,
                items_extracted=b.items_extracted,
                status=b.status,
                created_at=b.created_at.isoformat() if b.created_at else None,
                processed_at=b.processed_at.isoformat() if b.processed_at else None,
            )
            for b in batches
        ]


@router.post("/compact", response_model=dict, summary="Trigger memory compaction")
async def trigger_compaction(chat_id: int | None = Query(None)) -> dict:
    """Manually trigger memory compaction."""
    from src.memory_services.compaction_service import compaction_service

    if chat_id:
        results = await compaction_service.compact_chat(chat_id)
        return {"status": "success", "chat_id": chat_id, "results": len(results)}
    else:
        results = await compaction_service.compact_all_chats()
        return {"status": "success", "chats_compacted": len(results)}


class ClearChatRequest(BaseModel):
    chat_id: int


class ClearChatResponse(BaseModel):
    status: str
    chat_id: int
    deleted: dict


@router.post("/clear-chat", response_model=ClearChatResponse, summary="Full chat data cleanup")
async def clear_chat_full(request: ClearChatRequest) -> ClearChatResponse:
    """Полная очистка всех данных конкретного чата.

    Удаляет:
    - MemoryItem (факты, предпочтения, события)
    - MemorySummary (саммари сессий)
    - MemoryExtractionBatch (история экстракции)
    - UserProfile (профили пользователей в этом чате)
    - ChatVibeProfile (вайб чата)
    - Message (сообщения)
    - SkillState (активные сессии скиллов)
    """
    from src.database.models import (
        UserProfile, ChatVibeProfile, Message,
        SkillState, Reminder,
    )

    chat_id = request.chat_id
    deleted = {
        "memory_items": 0,
        "memory_summaries": 0,
        "extraction_batches": 0,
        "user_profiles": 0,
        "vibe_profile": 0,
        "messages": 0,
        "skill_states": 0,
        "reminders": 0,
    }

    async for session in get_session():
        # 1. Memory items
        stmt = select(MemoryItem).where(MemoryItem.chat_id == chat_id)
        result = await session.execute(stmt)
        items = list(result.scalars().all())
        deleted["memory_items"] = len(items)
        for item in items:
            await session.delete(item)

        # 2. Memory summaries
        stmt = select(MemorySummary).where(MemorySummary.chat_id == chat_id)
        result = await session.execute(stmt)
        summaries = list(result.scalars().all())
        deleted["memory_summaries"] = len(summaries)
        for s in summaries:
            await session.delete(s)

        # 3. Extraction batches
        stmt = select(MemoryExtractionBatch).where(MemoryExtractionBatch.chat_id == chat_id)
        result = await session.execute(stmt)
        batches = list(result.scalars().all())
        deleted["extraction_batches"] = len(batches)
        for b in batches:
            await session.delete(b)

        # 4. User profiles (только для этого чата)
        stmt = select(UserProfile).where(UserProfile.chat_id == chat_id)
        result = await session.execute(stmt)
        profiles = list(result.scalars().all())
        deleted["user_profiles"] = len(profiles)
        for p in profiles:
            await session.delete(p)

        # 5. Vibe profile
        stmt = select(ChatVibeProfile).where(ChatVibeProfile.chat_id == chat_id)
        result = await session.execute(stmt)
        vibes = list(result.scalars().all())
        deleted["vibe_profile"] = len(vibes)
        for v in vibes:
            await session.delete(v)

        # 6. Messages
        stmt = select(Message).where(Message.chat_id == chat_id)
        result = await session.execute(stmt)
        messages = list(result.scalars().all())
        deleted["messages"] = len(messages)
        for m in messages:
            await session.delete(m)

        # 7. Skill states
        stmt = select(SkillState).where(SkillState.chat_id == chat_id)
        result = await session.execute(stmt)
        skills = list(result.scalars().all())
        deleted["skill_states"] = len(skills)
        for s in skills:
            await session.delete(s)

        # 8. Reminders
        from src.database.models import Reminder
        stmt = select(Reminder).where(Reminder.chat_id == chat_id)
        result = await session.execute(stmt)
        reminders = list(result.scalars().all())
        deleted["reminders"] = len(reminders)
        for r in reminders:
            await session.delete(r)

        await session.commit()

    return ClearChatResponse(
        status="cleared",
        chat_id=chat_id,
        deleted=deleted,
    )
