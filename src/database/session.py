from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=3600,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_async_session() -> AsyncSession:
    async with async_session_factory() as session:
        yield session


async def get_session():
    """Return a standalone session (for non-request contexts)."""
    async with async_session_factory() as session:
        yield session


# ========== SYNC SESSION — for Celery workers ==========
# Celery uses prefork which is incompatible with asyncpg's event loop.
# Use psycopg2 (sync driver) for all Celery tasks.

sync_engine = create_engine(
    settings.database_url_sync,
    echo=False,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    pool_recycle=3600,
)

sync_session_factory = sessionmaker(
    sync_engine,
    class_=Session,
    expire_on_commit=False,
)


def get_sync_session():
    """Return a sync session for Celery/background tasks."""
    with sync_session_factory() as session:
        yield session
