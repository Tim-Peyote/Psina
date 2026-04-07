from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from src.config import settings

logger = structlog.get_logger()


def create_app() -> FastAPI:
    """Create and configure the FastAPI admin application."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Admin API started", port=settings.admin_api_port)
        yield
        logger.info("Admin API stopped")

    app = FastAPI(
        title="Zalutka Admin API",
        version="0.1.0",
        lifespan=lifespan,
    )

    from src.admin_api.routes import memory, users, usage, settings as settings_route, skills

    app.include_router(memory.router, prefix="/api/memory", tags=["memory"])
    app.include_router(users.router, prefix="/api/users", tags=["users"])
    app.include_router(usage.router, prefix="/api/usage", tags=["usage"])
    app.include_router(settings_route.router, prefix="/api/settings", tags=["settings"])
    app.include_router(skills.router, prefix="/api/skills", tags=["skills"])

    @app.get("/health")
    async def health_check() -> dict:
        return {"status": "ok"}

    return app
