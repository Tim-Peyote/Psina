from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from src.skill_system.registry import skill_registry
from src.skill_system.state_manager import skill_state_manager
from src.database.models import Skill, SkillState, SkillEvent
from src.database.session import get_session
from sqlalchemy import select, and_, func

router = APIRouter()


class SkillResponse(BaseModel):
    slug: str
    name: str
    description: str
    version: str
    is_active: bool
    triggers: list[str] | None
    created_at: str | None


@router.get("/", response_model=list[SkillResponse])
async def list_skills(include_inactive: bool = False) -> list[SkillResponse]:
    """List all installed skills."""
    skills = await skill_registry.get_all_skills(include_inactive=include_inactive)
    return [
        SkillResponse(
            slug=s.slug,
            name=s.name,
            description=s.description,
            version=s.version,
            is_active=s.is_active,
            triggers=s.triggers,
            created_at=s.created_at.isoformat() if s.created_at else None,
        )
        for s in skills
    ]


@router.post("/install")
async def install_skill(
    slug: str,
    name: str,
    description: str,
    system_prompt: str,
    triggers: list[str] | None = None,
    version: str = "1.0.0",
) -> dict:
    """Install a new skill."""
    existing = skill_registry.get_skill(slug)
    if existing:
        raise HTTPException(status_code=409, detail=f"Skill '{slug}' already installed")

    await skill_registry.register_skill(
        slug=slug,
        name=name,
        description=description,
        system_prompt=system_prompt,
        triggers=triggers or [],
        version=version,
    )
    return {"status": "installed", "slug": slug}


@router.delete("/{slug}")
async def uninstall_skill(slug: str) -> dict:
    """Uninstall a skill."""
    result = await skill_registry.unregister_skill(slug)
    if not result:
        raise HTTPException(status_code=404, detail=f"Skill '{slug}' not found")
    return {"status": "uninstalled", "slug": slug}


@router.post("/{slug}/toggle")
async def toggle_skill(slug: str, active: bool = True) -> dict:
    """Enable or disable a skill."""
    result = await skill_registry.toggle_skill(slug, active)
    if not result:
        raise HTTPException(status_code=404, detail=f"Skill '{slug}' not found")
    return {"status": "toggled", "slug": slug, "active": active}


# ========== SKILL STATE ENDPOINTS ==========


class SkillStateResponse(BaseModel):
    id: int
    skill_slug: str
    chat_id: int
    is_active: bool
    last_activity_at: str | None


@router.get("/{slug}/state", response_model=list[SkillStateResponse])
async def get_skill_state(slug: str) -> list[SkillStateResponse]:
    """Get all active state instances for a skill."""
    async for session in get_session():
        stmt = select(SkillState).where(
            SkillState.skill_slug == slug
        ).order_by(SkillState.last_activity_at.desc()).limit(100)
        result = await session.execute(stmt)
        states = list(result.scalars().all())

        return [
            SkillStateResponse(
                id=s.id,
                skill_slug=s.skill_slug,
                chat_id=s.chat_id,
                is_active=s.is_active,
                last_activity_at=s.last_activity_at.isoformat() if s.last_activity_at else None,
            )
            for s in states
        ]


@router.delete("/{slug}/state/{chat_id}")
async def delete_skill_state(slug: str, chat_id: int) -> dict:
    """Delete skill state for a chat (reset)."""
    result = await skill_state_manager.delete_state(slug, chat_id)
    if not result:
        raise HTTPException(status_code=404, detail="State not found")
    return {"status": "deleted", "slug": slug, "chat_id": chat_id}


# ========== SKILL EVENTS ==========


class SkillEventResponse(BaseModel):
    id: int
    skill_slug: str
    chat_id: int
    event_type: str
    content: str | None
    created_at: str | None


@router.get("/events", response_model=list[SkillEventResponse])
async def list_skill_events(
    slug: str | None = Query(None),
    chat_id: int | None = Query(None),
    limit: int = Query(50, le=200),
) -> list[SkillEventResponse]:
    """List skill events."""
    async for session in get_session():
        stmt = select(SkillEvent)
        conditions = []
        if slug:
            conditions.append(SkillEvent.skill_slug == slug)
        if chat_id:
            conditions.append(SkillEvent.chat_id == chat_id)

        if conditions:
            stmt = stmt.where(and_(*conditions))

        stmt = stmt.order_by(SkillEvent.created_at.desc()).limit(limit)
        result = await session.execute(stmt)
        events = list(result.scalars().all())

        return [
            SkillEventResponse(
                id=e.id,
                skill_slug=e.skill_slug,
                chat_id=e.chat_id,
                event_type=e.event_type,
                content=e.content,
                created_at=e.created_at.isoformat() if e.created_at else None,
            )
            for e in events
        ]
