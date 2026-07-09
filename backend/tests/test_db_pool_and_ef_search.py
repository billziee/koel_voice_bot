"""O-10: pgvector ef_search knob + DB pool checkout observability (unit-level).

Proves, without a live Postgres:
  * retrieve() issues ``SET LOCAL hnsw.ef_search = N`` before the ANN query only
    when the setting > 0, and never at 0 (leave the server default) — asserted
    against a fake session that records every executed statement.
  * A SET LOCAL failure is swallowed and retrieval still runs (RAG is degrade-safe).
  * The pool checkout/checkin observability listeners attach to an engine and fire
    without error, recording a connection hold time on checkin.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from koel.nlp.rag import HashEmbedder, retrieve


class _FakeResult:
    def all(self):  # noqa: ANN201 — mirrors SQLAlchemy Result.all()
        return []


class _FakeSession:
    """Records every statement passed to ``execute`` so a test can assert whether
    the SET LOCAL was issued, without touching a real database."""

    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute(self, statement, *args, **kwargs):  # noqa: ANN001, ANN202
        self.executed.append(str(statement))
        return _FakeResult()


def _patch_retrieve(monkeypatch, *, ef_search: int) -> _FakeSession:
    """Point retrieve() at a fake session_scope + a fixed ef_search setting."""
    session = _FakeSession()

    @contextlib.asynccontextmanager
    async def _fake_scope():
        yield session

    monkeypatch.setattr("koel.nlp.rag.session_scope", _fake_scope)
    monkeypatch.setattr(
        "koel.nlp.rag.get_common_settings",
        lambda: SimpleNamespace(pgvector_hnsw_ef_search=ef_search),
    )
    return session


async def test_retrieve_sets_ef_search_when_positive(monkeypatch):
    session = _patch_retrieve(monkeypatch, ef_search=64)
    out = await retrieve("annual fee", rag_index="cc", embedder=HashEmbedder(), top_k=2)
    assert out == []  # the fake session returns no rows
    set_local = [s for s in session.executed if "hnsw.ef_search" in s]
    assert set_local, "expected a SET LOCAL hnsw.ef_search statement"
    assert "64" in set_local[0]
    # The SET LOCAL must precede the nearest-neighbour SELECT (same transaction).
    assert session.executed[0] == set_local[0]
    assert len(session.executed) == 2  # SET LOCAL + the SELECT


async def test_retrieve_skips_ef_search_when_zero(monkeypatch):
    session = _patch_retrieve(monkeypatch, ef_search=0)
    out = await retrieve("annual fee", rag_index="cc", embedder=HashEmbedder(), top_k=2)
    assert out == []
    assert not any("hnsw.ef_search" in s for s in session.executed)
    assert len(session.executed) == 1  # only the SELECT ran


async def test_retrieve_swallows_ef_search_failure(monkeypatch):
    """A SET LOCAL failure must not break retrieval (RAG is best-effort)."""
    session = _patch_retrieve(monkeypatch, ef_search=32)
    orig_execute = session.execute
    calls = {"n": 0}

    async def flaky_execute(statement, *a, **k):  # noqa: ANN001, ANN202
        calls["n"] += 1
        if "hnsw.ef_search" in str(statement):
            raise RuntimeError("SET LOCAL not supported here")
        return await orig_execute(statement, *a, **k)

    monkeypatch.setattr(session, "execute", flaky_execute)
    out = await retrieve("x", rag_index="cc", embedder=HashEmbedder(), top_k=1)
    assert out == []  # degraded past the SET LOCAL error, still ran the query
    assert calls["n"] == 2  # SET LOCAL (raised) + the SELECT


async def test_pool_observability_listeners_attach_and_fire(monkeypatch):
    from koel.db import session as session_mod

    in_use: list[int] = []
    held: list[float] = []
    monkeypatch.setattr(session_mod, "record_db_pool_in_use", in_use.append)
    monkeypatch.setattr(session_mod, "record_db_pool_held", held.append)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_mod._install_pool_metrics(engine)  # must attach without error
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    finally:
        await engine.dispose()

    # SQLite's StaticPool has no checkedout(), so in_use may stay empty (the count
    # sampling is best-effort); the hold timer runs on every pool type, so the
    # checkin listener must have recorded a non-negative duration.
    assert held, "checkin listener should record a connection hold time"
    assert held[0] >= 0.0
