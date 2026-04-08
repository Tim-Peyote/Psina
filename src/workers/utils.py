"""Shared utilities for Celery workers."""

import asyncio


def run_async(coro):
    """Run async code in Celery with a fresh event loop and isolated DB engine.

    Celery prefork forks create new processes without event loops.
    The module-level async engine is bound to the old (non-existent) loop.
    We create a fresh engine inside the new loop to avoid 'different loop' errors.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        from src.database import session as db_session
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
        from src.config import settings

        db_session.engine = create_async_engine(
            settings.database_url,
            echo=False,
            pool_size=2,
            max_overflow=5,
            pool_pre_ping=True,
            pool_recycle=3600,
        )
        db_session.async_session_factory = async_sessionmaker(
            db_session.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        result = loop.run_until_complete(coro)

        async def _close():
            await db_session.engine.dispose()
        loop.run_until_complete(_close())

        return result
    finally:
        loop.close()
