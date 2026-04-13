from contextlib import asynccontextmanager

import structlog
from fastapi import Depends, FastAPI
from pydantic import BaseModel

from src.admin_api.auth import verify_admin_token
from src.config import settings

logger = structlog.get_logger()


def create_app() -> FastAPI:
    """Create and configure the FastAPI admin application."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.admin_api_secret == "change_me_in_production":
            logger.critical(
                "Admin API secret is default! Change ADMIN_API_SECRET in .env"
            )
        logger.info("Admin API started", port=settings.admin_api_port)
        yield
        logger.info("Admin API stopped")

    app = FastAPI(
        title="Zalutka Admin API",
        version="0.1.0",
        lifespan=lifespan,
        description="Admin interface for managing the Zalutka bot — skills, memory, users, settings, and usage stats.",
    )

    # All API routes require Bearer token authentication
    auth_dep = [Depends(verify_admin_token)]

    from src.admin_api.routes import memory, users, usage, settings as settings_route, skills

    app.include_router(memory.router, prefix="/api/memory", dependencies=auth_dep)
    app.include_router(users.router, prefix="/api/users", dependencies=auth_dep)
    app.include_router(usage.router, prefix="/api/usage", dependencies=auth_dep)
    app.include_router(settings_route.router, prefix="/api/settings", dependencies=auth_dep)
    app.include_router(skills.router, prefix="/api/skills", dependencies=auth_dep)

    # ===== Overview endpoint =====

    class OverviewResponse(BaseModel):
        total_users: int
        total_memory_items: int
        active_skills: int
        llm_provider: str
        llm_model: str
        db_status: str
        redis_status: str

    @app.get("/api/overview", response_model=OverviewResponse, summary="System overview", dependencies=auth_dep)
    async def overview() -> OverviewResponse:
        """Quick summary of system state."""
        from src.database.session import get_session
        from src.database.models import User, MemoryItem
        from src.skill_system.registry import skill_registry
        from sqlalchemy import select, func

        # Users count
        async for session in get_session():
            stmt = select(func.count(User.id))
            result = await session.execute(stmt)
            total_users = result.scalar() or 0
            break

        # Memory items count
        async for session in get_session():
            stmt = select(func.count(MemoryItem.id))
            result = await session.execute(stmt)
            total_memory_items = result.scalar() or 0
            break

        # Active skills
        all_skills = skill_registry.get_discovered()
        active_skills = len(all_skills)

        # DB status
        db_status = "ok"
        try:
            from sqlalchemy import text
            async for session in get_session():
                await session.execute(text("SELECT 1"))
        except Exception:
            db_status = "error"

        # Redis status
        redis_status = "ok"
        try:
            import redis.asyncio as aioredis
            r = aioredis.from_url(settings.redis_url, decode_responses=True)
            await r.ping()
            await r.aclose()
        except Exception:
            redis_status = "error"

        return OverviewResponse(
            total_users=total_users,
            total_memory_items=total_memory_items,
            active_skills=active_skills,
            llm_provider=settings.llm_provider,
            llm_model=settings.llm_model,
            db_status=db_status,
            redis_status=redis_status,
        )

    @app.get("/health")
    async def health_check() -> dict:
        status = {"status": "ok", "components": {}}

        # Check DB
        try:
            from src.database.session import get_session
            from sqlalchemy import text
            async for session in get_session():
                await session.execute(text("SELECT 1"))
            status["components"]["database"] = "ok"
        except Exception as e:
            status["components"]["database"] = f"error: {e}"
            status["status"] = "degraded"

        # Check Redis
        try:
            import redis.asyncio as aioredis
            r = aioredis.from_url(settings.redis_url, decode_responses=True)
            await r.ping()
            await r.aclose()
            status["components"]["redis"] = "ok"
        except Exception as e:
            status["components"]["redis"] = f"error: {e}"
            status["status"] = "degraded"

        return status

    @app.get("/api/debug/skill-state")
    async def debug_skill_state(chat_id: int) -> dict:
        """Debug endpoint: show RPG skill state for a chat (supports multi-session v2 format)."""
        from src.database.models import SkillState
        from src.database.session import get_session
        from sqlalchemy import select

        async for session in get_session():
            stmt = select(SkillState).where(
                SkillState.chat_id == chat_id,
                SkillState.skill_slug == "agent_rpg",
            )
            result = await session.execute(stmt)
            record = result.scalar_one_or_none()

            if record:
                state = record.state_json or {}
                # Multi-session v2 format
                if "sessions" in state:
                    active_id = state.get("active_session_id")
                    sessions_summary = {}
                    for sid, s in state.get("sessions", {}).items():
                        sessions_summary[sid] = {
                            "name": s.get("name"),
                            "phase": s.get("phase"),
                            "step": s.get("step"),
                            "system": s.get("system"),
                            "players": s.get("players", []),
                            "characters": list(s.get("characters", {}).keys()),
                            "world_setting": s.get("world", {}).get("setting"),
                            "is_active": sid == active_id,
                        }
                    return {
                        "found": True,
                        "format": "v2_multi_session",
                        "is_active": record.is_active,
                        "active_session_id": active_id,
                        "sessions_count": len(sessions_summary),
                        "sessions": sessions_summary,
                        "full_state": state,
                    }
                # Legacy v1 flat format
                return {
                    "found": True,
                    "format": "v1_legacy",
                    "is_active": record.is_active,
                    "phase": state.get("phase"),
                    "step": state.get("step"),
                    "characters": list(state.get("characters", {}).keys()),
                    "world_setting": state.get("world", {}).get("setting"),
                    "full_state": state,
                }
            return {"found": False, "chat_id": chat_id}

        return {"error": "no_db_session"}

    return app
