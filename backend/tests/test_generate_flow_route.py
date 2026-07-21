"""Route-level tests for POST /v1/knowledge/generate-flow (auto node-builder, 6.6).

koel/nlp/flow_gen.py's generate_flow()/_clean_flow() are unit-tested in
test_flow_gen.py. This file covers the HTTP route itself: auth, the 404/503
error states, and org-scoping — none of which touch a real pgvector column, so
(unlike the KnowledgeChunk write path — see test_knowledge_upload.py) these run
hermetically by faking session_scope, the same pattern test_db_pool_and_ef_search
uses for koel.nlp.rag.
"""

from __future__ import annotations

import contextlib

from koel.common.auth import issue_token
from koel.db.models import UseCase
from koel.voicebot.api import knowledge as knowledge_api

_ROUTE = "/v1/knowledge/generate-flow"

# A minimal valid 2-node flow — stands in for generate_flow()'s draft output
# wherever a test only cares that SOME flow came back, not its shape.
_SIMPLE_FLOW = {
    "start": "a",
    "nodes": {"a": {"prompt": "hi", "branches": {}}, "b": {"prompt": "bye", "branches": {}}},
}


class _FakeChunk:
    def __init__(self, *, title: str, content: str, use_case: UseCase = UseCase.credit_card) -> None:
        self.title = title
        self.content = content
        self.use_case = use_case


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self):  # noqa: ANN201 — mirrors SQLAlchemy Result.scalars()
        return self

    def all(self):  # noqa: ANN201
        return self._rows


class _FakeSession:
    """Records every compiled statement so a test can assert the org filter was
    (or wasn't) applied, without a real database."""

    def __init__(self, rows: list) -> None:
        self._rows = rows
        self.executed: list[str] = []

    async def execute(self, statement, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
        self.executed.append(str(statement))
        return _FakeResult(self._rows)


def _patch_db(monkeypatch, *, rows: list) -> _FakeSession:
    session = _FakeSession(rows)

    @contextlib.asynccontextmanager
    async def _fake_scope():
        yield session

    monkeypatch.setattr(knowledge_api, "session_scope", _fake_scope)
    return session


def _headers(*, org_id: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token('op_1', kind='access', role='operator', org_id=org_id)}"}


async def test_generate_flow_requires_auth(app_client, monkeypatch, test_db):
    # No token: CurrentUser's dependency must reject before the (patched) DB is
    # ever touched. Patch anyway so a bug that reordered auth after the DB query
    # fails loudly (real SQLite has no knowledge_chunks table) instead of masking
    # as a coincidental 401.
    client, _ = app_client
    _patch_db(monkeypatch, rows=[])
    r = await client.post(_ROUTE, json={"rag_index": "cc_hdfc"})
    assert r.status_code == 401


async def test_generate_flow_no_kb_content_is_404(app_client, monkeypatch, test_db):
    client, _ = app_client
    _patch_db(monkeypatch, rows=[])
    r = await client.post(_ROUTE, headers=_headers(), json={"rag_index": "cc_hdfc"})
    assert r.status_code == 404 and r.json()["code"] == "no_kb_content"


async def test_generate_flow_generation_unavailable_is_503(app_client, monkeypatch, test_db):
    client, _ = app_client
    _patch_db(monkeypatch, rows=[_FakeChunk(title="Card", content="No annual fee.")])

    async def _none(**_kw):  # noqa: ANN003, ANN202 — LLM unconfigured / draft unparseable
        return None

    monkeypatch.setattr(knowledge_api, "generate_flow", _none)
    r = await client.post(_ROUTE, headers=_headers(), json={"rag_index": "cc_hdfc"})
    assert r.status_code == 503 and r.json()["code"] == "generation_unavailable"


async def test_generate_flow_happy_path_defaults_use_case_from_chunk(app_client, monkeypatch, test_db):
    client, _ = app_client
    _patch_db(
        monkeypatch,
        rows=[
            _FakeChunk(title="Card", content="No annual fee.", use_case=UseCase.credit_card),
            _FakeChunk(title="Rewards", content="2x points on fuel.", use_case=UseCase.credit_card),
        ],
    )
    captured: dict = {}

    async def _fake_generate(*, chunks, use_case, language, settings):  # noqa: ANN001, ANN202
        captured["chunks"] = chunks
        captured["use_case"] = use_case
        captured["language"] = language
        return _SIMPLE_FLOW

    monkeypatch.setattr(knowledge_api, "generate_flow", _fake_generate)
    r = await client.post(_ROUTE, headers=_headers(), json={"rag_index": "cc_hdfc", "language": "hi"})

    assert r.status_code == 200
    body = r.json()
    assert body["node_count"] == 2 and body["source_chunks"] == 2
    assert body["flow"]["start"] == "a"
    # payload.use_case was omitted → falls back to the first row's use_case.
    assert captured["use_case"] == "credit_card"
    assert captured["language"] == "hi"
    assert captured["chunks"] == [
        {"title": "Card", "content": "No annual fee."},
        {"title": "Rewards", "content": "2x points on fuel."},
    ]


async def test_generate_flow_explicit_use_case_overrides_chunk_default(app_client, monkeypatch, test_db):
    client, _ = app_client
    _patch_db(
        monkeypatch, rows=[_FakeChunk(title="Policy", content="Sum assured.", use_case=UseCase.insurance)]
    )
    captured: dict = {}

    async def _fake_generate(*, chunks, use_case, language, settings):  # noqa: ANN001, ANN202
        captured["use_case"] = use_case
        return _SIMPLE_FLOW

    monkeypatch.setattr(knowledge_api, "generate_flow", _fake_generate)
    r = await client.post(
        _ROUTE, headers=_headers(), json={"rag_index": "ins_1", "use_case": "custom"}
    )
    assert r.status_code == 200
    assert captured["use_case"] == "custom"  # explicit payload value wins over the chunk's own use_case


async def test_generate_flow_is_org_scoped_in_the_query(app_client, monkeypatch, test_db):
    # Regression-shaped: assert the org filter is actually IN the compiled SQL
    # (not just that a fake session happens to return the "right" rows — the fake
    # session ignores its own WHERE clause, so the only honest assertion here is
    # on the statement text, mirroring test_db_pool_and_ef_search's convention).
    client, _ = app_client
    scoped_session = _patch_db(monkeypatch, rows=[_FakeChunk(title="A", content="B")])

    async def _fake_generate(**_kw):  # noqa: ANN003, ANN202
        return _SIMPLE_FLOW

    monkeypatch.setattr(knowledge_api, "generate_flow", _fake_generate)

    # "knowledge_chunks.org_id" alone isn't a safe marker — SQLAlchemy's compiled
    # SELECT lists every column of the entity regardless of any filter, so the
    # bare column name appears either way. The WHERE-clause equality
    # ("org_id = ...") only appears when the filter was actually applied.
    org_filter = "knowledge_chunks.org_id = "

    r = await client.post(_ROUTE, headers=_headers(org_id="orgA"), json={"rag_index": "cc_hdfc"})
    assert r.status_code == 200
    assert any(org_filter in stmt for stmt in scoped_session.executed)

    # A platform-admin token (no org_id) must NOT add the org filter — it can see
    # every tenant's KB content when drafting a flow.
    unscoped_session = _patch_db(monkeypatch, rows=[_FakeChunk(title="A", content="B")])
    r = await client.post(_ROUTE, headers=_headers(org_id=None), json={"rag_index": "cc_hdfc"})
    assert r.status_code == 200
    assert not any(org_filter in stmt for stmt in unscoped_session.executed)
