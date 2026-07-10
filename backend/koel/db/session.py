"""Async SQLAlchemy engine + session helpers."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from koel.common.config import get_common_settings
from koel.common.telemetry import record_db_pool_held, record_db_pool_in_use

# Stash key for the checkout timestamp on a connection record so the checkin
# listener can compute how long that connection was held.
_CHECKOUT_TS_KEY = "koel_pool_checkout_ts"


def _install_pool_metrics(engine: AsyncEngine) -> None:
    """Attach checkout/checkin listeners so pool pressure is observable under load
    (roadmap O-10): the in-use connection count sampled at checkout and how long
    each connection stays checked out. Best-effort — the telemetry helpers no-op
    when metrics aren't configured, and any listener error is swallowed so
    observability can never break a real DB checkout/checkin.

    Events fire on the pool wrapped by the async engine, reached via
    ``engine.sync_engine`` (SQLAlchemy drives async DBAPIs over a sync core).
    """
    sync_engine = engine.sync_engine
    pool = sync_engine.pool

    @event.listens_for(sync_engine, "checkout")
    def _on_checkout(dbapi_conn, conn_record, conn_proxy):  # noqa: ANN001, ANN202
        conn_record.info[_CHECKOUT_TS_KEY] = time.perf_counter()
        try:
            # QueuePool (Postgres) exposes checkedout(); StaticPool (SQLite tests)
            # doesn't, so this is best-effort — the hold timer below always runs.
            record_db_pool_in_use(pool.checkedout())
        except Exception:  # noqa: BLE001 — observability must never break checkout
            pass

    @event.listens_for(sync_engine, "checkin")
    def _on_checkin(dbapi_conn, conn_record):  # noqa: ANN001, ANN202
        started = conn_record.info.pop(_CHECKOUT_TS_KEY, None)
        if started is None:
            return
        try:
            record_db_pool_held((time.perf_counter() - started) * 1000.0)
        except Exception:  # noqa: BLE001 — observability must never break checkin
            pass


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
            # LIFO checkout lets idle connections age out between bursts (paired
            # with pool_pre_ping above), shrinking the live-connection footprint
            # against RDS max_connections. See config.db_pool_use_lifo.
            pool_use_lifo=settings.db_pool_use_lifo,
        )
    engine = create_async_engine(settings.database_url, **kwargs)
    # Pool checkout observability (O-10). Best-effort: never fail engine creation.
    try:
        _install_pool_metrics(engine)
    except Exception:  # noqa: BLE001 — observability is optional; keep the engine
        pass
    return engine


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
