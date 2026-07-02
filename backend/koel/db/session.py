"""Async SQLAlchemy engine + session helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from koel.common.config import get_common_settings


@lru_cache
def get_engine() -> AsyncEngine:
    settings = get_common_settings()
    kwargs: dict = {
        "echo": settings.debug and settings.log_level == "DEBUG",
        "pool_pre_ping": True,  # reap connections killed by the DB/idle proxies
    }
    # Real connection pooling for Postgres only. SQLite's async pool (tests)
    # rejects these kwargs, so we apply them by dialect.
    if settings.database_url.startswith("postgresql"):
        kwargs.update(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_recycle=settings.db_pool_recycle_s,
            pool_timeout=settings.db_pool_timeout_s,
        )
    return create_async_engine(settings.database_url, **kwargs)


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope: commit on success, rollback on error."""
    maker = get_sessionmaker()
    async with maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
