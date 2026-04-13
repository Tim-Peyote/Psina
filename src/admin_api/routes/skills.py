from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from src.skill_system.registry import skill_registry
from src.skill_system.state_manager import skill_state_manager
from src.database.models import Skill, SkillState, SkillEvent, Reminder
from src.database.session import get_session
from sqlalchemy import select, and_, func

router = APIRouter(tags=["skills"])


class SkillResponse(BaseModel):
    slug: str
    name: str
    description: str
    version: str
    is_active: bool
    triggers: list[str] | None
    created_at: str | None


@router.get("/", response_model=list[SkillResponse], summary="List all installed skills")
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


class InstallSkillRequest(BaseModel):
    slug: str
    name: str
    description: str
    system_prompt: str
    triggers: list[str] | None = None
    version: str = "1.0.0"


@router.post("/install", summary="Install a new skill")
async def install_skill(request: InstallSkillRequest) -> dict:
    """Install a new skill into the system."""
    existing = skill_registry.get_skill(request.slug)
    if existing:
        raise HTTPException(status_code=409, detail=f"Skill '{request.slug}' already installed")

    await skill_registry.register_skill(
        slug=request.slug,
        name=request.name,
        description=request.description,
        system_prompt=request.system_prompt,
        triggers=request.triggers or [],
        version=request.version,
    )
    return {"status": "installed", "slug": request.slug}


@router.delete("/{slug}", summary="Uninstall a skill")
async def uninstall_skill(slug: str) -> dict:
    """Uninstall a skill."""
    result = await skill_registry.unregister_skill(slug)
    if not result:
        raise HTTPException(status_code=404, detail=f"Skill '{slug}' not found")
    return {"status": "uninstalled", "slug": slug}


@router.post("/{slug}/toggle", summary="Enable or disable a skill")
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


@router.get("/{slug}/state", response_model=list[SkillStateResponse], summary="Get skill state instances")
async def get_skill_state(slug: str) -> list[SkillStateResponse]:
    """Get all state instances for a skill."""
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


@router.delete("/{slug}/state/{chat_id}", summary="Delete skill state for a chat (reset)")
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


@router.get("/events", response_model=list[SkillEventResponse], summary="List skill events")
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


# ========== RPG SESSIONS ==========


class RpgSessionResponse(BaseModel):
    session_id: str
    name: str
    phase: str
    system: str
    players_count: int
    created_by: int
    is_active: bool


@router.get("/agent_rpg/sessions", response_model=list[RpgSessionResponse], summary="List RPG sessions for a chat")
async def list_rpg_sessions(chat_id: int = Query(..., description="Chat ID")) -> list[RpgSessionResponse]:
    """List all RPG campaign sessions for a specific chat."""
    state = await skill_state_manager.get_state("agent_rpg", chat_id, default={})
    if not isinstance(state, dict) or "sessions" not in state:
        return []

    active_id = state.get("active_session_id")
    sessions = state.get("sessions", {})

    return [
        RpgSessionResponse(
            session_id=sid,
            name=s.get("name", sid),
            phase=s.get("phase", "unknown"),
            system=s.get("system", ""),
            players_count=len(s.get("players", [])),
            created_by=s.get("created_by", 0),
            is_active=(sid == active_id),
        )
        for sid, s in sessions.items()
    ]


@router.delete("/agent_rpg/sessions/{session_id}", summary="Delete a specific RPG session")
async def delete_rpg_session(session_id: str, chat_id: int = Query(...)) -> dict:
    """Delete a specific RPG campaign session from a chat's state."""
    state = await skill_state_manager.get_state("agent_rpg", chat_id, default={})
    if not isinstance(state, dict) or session_id not in state.get("sessions", {}):
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found for chat {chat_id}")

    del state["sessions"][session_id]
    if state.get("active_session_id") == session_id:
        state["active_session_id"] = None

    await skill_state_manager.set_state("agent_rpg", chat_id, state)
    return {"status": "deleted", "session_id": session_id, "chat_id": chat_id}


# ========== NOTES ==========


class NoteResponse(BaseModel):
    id: str
    title: str
    content: str
    tags: list[str]
    user_id: int
    created_at: str | None


@router.get("/agent_notes/notes", response_model=list[NoteResponse], summary="List notes for a chat")
async def list_notes(chat_id: int = Query(..., description="Chat ID")) -> list[NoteResponse]:
    """List all notes stored by agent_notes for a specific chat."""
    state = await skill_state_manager.get_state("agent_notes", chat_id, default={"notes": []})
    if not isinstance(state, dict):
        return []

    notes = state.get("notes", [])
    return [
        NoteResponse(
            id=n.get("id", ""),
            title=n.get("title", ""),
            content=n.get("content", ""),
            tags=n.get("tags", []),
            user_id=n.get("user_id", 0),
            created_at=n.get("created_at"),
        )
        for n in notes
    ]


@router.delete("/agent_notes/notes/{note_id}", summary="Delete a specific note")
async def delete_note(note_id: str, chat_id: int = Query(...)) -> dict:
    """Delete a specific note from a chat's notes state."""
    state = await skill_state_manager.get_state("agent_notes", chat_id, default={"notes": []})
    if not isinstance(state, dict):
        raise HTTPException(status_code=404, detail="Notes state not found")

    notes = state.get("notes", [])
    original_count = len(notes)
    state["notes"] = [n for n in notes if n.get("id") != note_id]

    if len(state["notes"]) == original_count:
        raise HTTPException(status_code=404, detail=f"Note '{note_id}' not found")

    await skill_state_manager.set_state("agent_notes", chat_id, state)
    return {"status": "deleted", "note_id": note_id, "chat_id": chat_id}


# ========== REMINDERS ==========


class ReminderResponse(BaseModel):
    id: int
    chat_id: int
    user_id: int
    target_user_id: int | None
    content: str
    remind_at: str
    is_sent: bool
    created_at: str | None


@router.get("/agent_reminders/active", response_model=list[ReminderResponse], summary="List active reminders for a chat")
async def list_active_reminders(
    chat_id: int = Query(..., description="Chat ID"),
    include_sent: bool = Query(False),
) -> list[ReminderResponse]:
    """List reminders for a specific chat."""
    async for session in get_session():
        stmt = select(Reminder).where(Reminder.chat_id == chat_id)
        if not include_sent:
            stmt = stmt.where(Reminder.is_sent == False)
        stmt = stmt.order_by(Reminder.remind_at)
        result = await session.execute(stmt)
        reminders = list(result.scalars().all())

        return [
            ReminderResponse(
                id=r.id,
                chat_id=r.chat_id,
                user_id=r.user_id,
                target_user_id=r.target_user_id,
                content=r.content,
                remind_at=r.remind_at.isoformat() if r.remind_at else "",
                is_sent=r.is_sent,
                created_at=r.created_at.isoformat() if r.created_at else None,
            )
            for r in reminders
        ]
