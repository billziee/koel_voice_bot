"""Route-level tests for the knowledge-chunk CRUD + index-summary endpoints
(``/v1/knowledge-indexes``, ``/v1/knowledge-chunks`` GET/POST,
``/v1/knowledge-chunks/{id}`` DELETE).

``KnowledgeChunk`` uses the pgvector ``Vector`` column type, so (like
``test_generate_flow_route.py``) these run hermetically by faking
``session_scope`` rather than touching the real SQLite test DB (which
deliberately excludes this table — see ``conftest._SQLITE_TABLES``) or a real
embedder (which would otherwise hit the configured NVIDIA endpoint over the
network). Constructing plain ``KnowledgeChunk`` instances and handing them to
the fake session is safe: nothing here binds to an engine, so the pgvector
type is never actually exercised.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime

from koel.common.auth import issue_token
from koel.db.models import EMBEDDING_DIM, KnowledgeChunk, UseCase
from koel.voicebot.api import knowledge as knowledge_api

_ORG_FILTER = "knowledge_chunks.org_id = "
_RAG_FILTER = "knowledge_chunks.rag_index = "


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self):  # noqa: ANN201 — mirrors SQLAlchemy Result.scalars()
        return self

    def all(self):  # noqa: ANN201
        return self._rows


class _FakeSession:
    """Records every compiled statement (so tests can assert org/index filters
    were applied) and every ``add``/``flush``/``get`` call (so tests can assert
    what would actually have been persisted or deleted), without a real DB."""

    def __init__(self, *, rows: list | None = None, get_row: KnowledgeChunk | None = None) -> None:
        self._rows = rows if rows is not None else []
        self._get_row = get_row
        self.executed: list[str] = []
        self.added: list[KnowledgeChunk] = []
        self.flush_count = 0

    async def execute(self, statement, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
        self.executed.append(str(statement))
        return _FakeResult(self._rows)

    def add(self, row: KnowledgeChunk) -> None:
        # Mirrors the DB-assigned id default (KnowledgeChunk.id has default=_uuid,
        # which only applies at INSERT time, not at plain construction) so the
        # response model's required `id: str` field has something to validate.
        if getattr(row, "id", None) is None:
            row.id = f"chunk_{len(self.added) + 1}"
        self.added.append(row)

    async def flush(self) -> None:
        self.flush_count += 1

    async def get(self, model, ident):  # noqa: ANN001, ANN201
        return self._get_row


def _patch_db(
    monkeypatch, *, rows: list | None = None, get_row: KnowledgeChunk | None = None
) -> _FakeSession:
    session = _FakeSession(rows=rows, get_row=get_row)

    @contextlib.asynccontextmanager
    async def _fake_scope():
        yield session

    monkeypatch.setattr(knowledge_api, "session_scope", _fake_scope)
    return session


class _FakeEmbedder:
    """Records embed() calls; never touches the network (unlike the real
    NVIDIA-backed embedder ``_get_embedder()`` would build in this env)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def embed(self, text: str, *, input_type: str = "query") -> list[float]:
        self.calls.append((text, input_type))
        return [0.1, 0.2, 0.3]


def _patch_embedder(monkeypatch) -> _FakeEmbedder:
    embedder = _FakeEmbedder()
    monkeypatch.setattr(knowledge_api, "_get_embedder", lambda: embedder)
    return embedder


def _headers(*, org_id: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token('op_1', kind='access', role='operator', org_id=org_id)}"}


def _make_chunk(**overrides) -> KnowledgeChunk:
    fields = dict(
        id="chunk_1",
        org_id="org_a",
        rag_index="cc_hdfc",
        use_case=UseCase.credit_card,
        language="hinglish",
        title="Annual Fee",
        content="No annual fee for the first year.",
        embedding=[0.0] * EMBEDDING_DIM,
        meta={},
        created_at=datetime.now(UTC),
    )
    fields.update(overrides)
    return KnowledgeChunk(**fields)


# --- GET /v1/knowledge-indexes -----------------------------------------------


async def test_list_indexes_returns_summary(app_client, monkeypatch, test_db):
    client, _ = app_client
    _patch_db(monkeypatch, rows=[("cc_hdfc", 3), ("ins_1", 1)])
    r = await client.get("/v1/knowledge-indexes", headers=_headers(org_id="org_a"))
    assert r.status_code == 200
    assert r.json() == [
        {"rag_index": "cc_hdfc", "chunk_count": 3},
        {"rag_index": "ins_1", "chunk_count": 1},
    ]


async def test_list_indexes_is_org_scoped(app_client, monkeypatch, test_db):
    client, _ = app_client
    scoped = _patch_db(monkeypatch, rows=[("cc_hdfc", 3)])
    r = await client.get("/v1/knowledge-indexes", headers=_headers(org_id="org_a"))
    assert r.status_code == 200
    assert any(_ORG_FILTER in stmt for stmt in scoped.executed)

    # A platform-admin / single-tenant token (no org_id) sees every tenant's
    # indexes — the filter must NOT be added.
    unscoped = _patch_db(monkeypatch, rows=[("cc_hdfc", 3)])
    r = await client.get("/v1/knowledge-indexes", headers=_headers(org_id=None))
    assert r.status_code == 200
    assert not any(_ORG_FILTER in stmt for stmt in unscoped.executed)


# --- GET /v1/knowledge-chunks -------------------------------------------------


async def test_list_chunks_maps_rows_to_response_model(app_client, monkeypatch, test_db):
    client, _ = app_client
    row = _make_chunk()
    _patch_db(monkeypatch, rows=[row])
    r = await client.get("/v1/knowledge-chunks", headers=_headers(org_id="org_a"))
    assert r.status_code == 200
    assert r.json() == [
        {
            "id": "chunk_1",
            "rag_index": "cc_hdfc",
            "use_case": "credit_card",
            "language": "hinglish",
            "title": "Annual Fee",
            "content": "No annual fee for the first year.",
        }
    ]


async def test_list_chunks_applies_rag_index_filter_only_when_given(app_client, monkeypatch, test_db):
    client, _ = app_client
    row = _make_chunk()
    with_filter = _patch_db(monkeypatch, rows=[row])
    r = await client.get(
        "/v1/knowledge-chunks", headers=_headers(org_id="org_a"), params={"rag_index": "cc_hdfc"}
    )
    assert r.status_code == 200
    assert any(_RAG_FILTER in stmt for stmt in with_filter.executed)

    without_filter = _patch_db(monkeypatch, rows=[row])
    r = await client.get("/v1/knowledge-chunks", headers=_headers(org_id="org_a"))
    assert r.status_code == 200
    assert not any(_RAG_FILTER in stmt for stmt in without_filter.executed)


async def test_list_chunks_is_org_scoped(app_client, monkeypatch, test_db):
    client, _ = app_client
    row = _make_chunk()
    scoped = _patch_db(monkeypatch, rows=[row])
    r = await client.get("/v1/knowledge-chunks", headers=_headers(org_id="org_a"))
    assert r.status_code == 200
    assert any(_ORG_FILTER in stmt for stmt in scoped.executed)

    unscoped = _patch_db(monkeypatch, rows=[row])
    r = await client.get("/v1/knowledge-chunks", headers=_headers(org_id=None))
    assert r.status_code == 200
    assert not any(_ORG_FILTER in stmt for stmt in unscoped.executed)


# --- POST /v1/knowledge-chunks -----------------------------------------------


async def test_create_chunk_single_persists_and_embeds_title_plus_content(app_client, monkeypatch, test_db):
    client, _ = app_client
    session = _patch_db(monkeypatch, rows=[])
    embedder = _patch_embedder(monkeypatch)
    payload = {
        "rag_index": "cc_hdfc",
        "use_case": "credit_card",
        "language": "hinglish",
        "title": "Annual Fee",
        "content": "No annual fee for the first year.",
        "chunk": False,
    }
    r = await client.post("/v1/knowledge-chunks", headers=_headers(org_id="org_a"), json=payload)
    assert r.status_code == 201
    body = r.json()
    assert len(body) == 1
    assert body[0]["title"] == "Annual Fee"
    assert body[0]["content"] == payload["content"]
    assert body[0]["rag_index"] == "cc_hdfc"
    # The stored row carries the caller's org (tenant-scoped write).
    assert session.added[0].org_id == "org_a"
    assert session.flush_count == 1
    # Embedded text is "{title} {content}" as a passage (asymmetric embedders
    # embed queries and documents differently).
    assert embedder.calls == [("Annual Fee No annual fee for the first year.", "passage")]


async def test_create_chunks_split_numbers_titles_and_embeds_each_body(app_client, monkeypatch, test_db):
    client, _ = app_client
    session = _patch_db(monkeypatch, rows=[])
    embedder = _patch_embedder(monkeypatch)
    monkeypatch.setattr(knowledge_api, "chunk_text", lambda content: ["Part one.", "Part two."])
    payload = {
        "rag_index": "cc_hdfc",
        "use_case": "credit_card",
        "title": "Terms",
        "content": "a long document" * 50,
        "chunk": True,
    }
    r = await client.post("/v1/knowledge-chunks", headers=_headers(org_id=None), json=payload)
    assert r.status_code == 201
    body = r.json()
    assert [c["title"] for c in body] == ["Terms (1/2)", "Terms (2/2)"]
    assert [c["content"] for c in body] == ["Part one.", "Part two."]
    assert session.flush_count == 2
    assert len(embedder.calls) == 2
    # Unscoped (no org on the token) writes land with org_id=None.
    assert all(row.org_id is None for row in session.added)


async def test_create_chunks_no_bodies_is_400_before_any_write(app_client, monkeypatch, test_db):
    client, _ = app_client
    session = _patch_db(monkeypatch, rows=[])
    _patch_embedder(monkeypatch)
    monkeypatch.setattr(knowledge_api, "chunk_text", lambda content: [])
    payload = {
        "rag_index": "cc_hdfc",
        "use_case": "credit_card",
        "title": "Empty",
        "content": "   ",
        "chunk": True,
    }
    r = await client.post("/v1/knowledge-chunks", headers=_headers(org_id="org_a"), json=payload)
    assert r.status_code == 400
    assert r.json()["code"] == "empty_content"
    assert session.added == []  # the guard fires before any row is constructed


# --- DELETE /v1/knowledge-chunks/{chunk_id} ----------------------------------


async def test_delete_chunk_success_issues_the_delete(app_client, monkeypatch, test_db):
    client, _ = app_client
    row = _make_chunk(id="chunk_1", org_id="org_a")
    session = _patch_db(monkeypatch, get_row=row)
    r = await client.delete("/v1/knowledge-chunks/chunk_1", headers=_headers(org_id="org_a"))
    assert r.status_code == 204
    assert len(session.executed) == 1
    assert "DELETE FROM knowledge_chunks" in session.executed[0]


async def test_delete_chunk_missing_is_404_and_issues_no_delete(app_client, monkeypatch, test_db):
    client, _ = app_client
    session = _patch_db(monkeypatch, get_row=None)
    r = await client.delete("/v1/knowledge-chunks/missing", headers=_headers(org_id="org_a"))
    assert r.status_code == 404
    assert r.json()["code"] == "chunk_not_found"
    assert session.executed == []


async def test_delete_chunk_cross_tenant_is_404_not_403(app_client, monkeypatch, test_db):
    # Existence must not be leaked across tenants — org B's chunk looks exactly
    # like "missing" to org A, and (critically) is never actually deleted.
    client, _ = app_client
    row = _make_chunk(id="chunk_1", org_id="org_b")
    session = _patch_db(monkeypatch, get_row=row)
    r = await client.delete("/v1/knowledge-chunks/chunk_1", headers=_headers(org_id="org_a"))
    assert r.status_code == 404
    assert r.json()["code"] == "chunk_not_found"
    assert session.executed == []


async def test_delete_chunk_unscoped_admin_can_delete_any_org(app_client, monkeypatch, test_db):
    client, _ = app_client
    row = _make_chunk(id="chunk_1", org_id="org_b")
    session = _patch_db(monkeypatch, get_row=row)
    r = await client.delete("/v1/knowledge-chunks/chunk_1", headers=_headers(org_id=None))
    assert r.status_code == 204
    assert len(session.executed) == 1
