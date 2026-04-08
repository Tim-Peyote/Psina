from contextlib import asynccontextmanager

import structlog
from fastapi import Depends, FastAPI

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
    )

    # All API routes require Bearer token authentication
    auth_dep = [Depends(verify_admin_token)]

    from src.admin_api.routes import memory, users, usage, settings as settings_route, skills

    app.include_router(memory.router, prefix="/api/memory", tags=["memory"], dependencies=auth_dep)
    app.include_router(users.router, prefix="/api/users", tags=["users"], dependencies=auth_dep)
    app.include_router(usage.router, prefix="/api/usage", tags=["usage"], dependencies=auth_dep)
    app.include_router(settings_route.router, prefix="/api/settings", tags=["settings"], dependencies=auth_dep)
    app.include_router(skills.router, prefix="/api/skills", tags=["skills"], dependencies=auth_dep)

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

    return app
