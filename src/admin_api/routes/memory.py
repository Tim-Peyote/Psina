from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from src.database.session import get_session
from src.database.models import MemoryItem, MemoryType
from sqlalchemy import select, and_

router = APIRouter()


class MemoryItemResponse(BaseModel):
    id: int
    chat_id: int | None
    user_id: int | None
    type: str
    content: str
    confidence: float
    relevance: float
    source: str

    class Config:
        from_attributes = True


class MemoryListResponse(BaseModel):
    items: list[MemoryItemResponse]
    total: int


@router.get("/", response_model=MemoryListResponse)
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
                    source=item.source,
                )
                for item in items
            ],
            total=total,
        )


@router.delete("/{item_id}")
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


@router.post("/clear")
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
