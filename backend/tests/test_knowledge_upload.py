"""File-upload KB ingestion (X): text/PDF extraction + endpoint validation.

``KnowledgeChunk`` uses the pgvector ``Vector`` column type (not creatable on the
SQLite test DB — conftest excludes it) and a real embed() call would otherwise hit
the NVIDIA endpoint configured in this env, so the DB-touching success path is
also verified live/in CI's Postgres service run. Here we cover the pure text
extraction, the pre-DB validation the endpoint enforces, AND (by faking
``session_scope`` + the embedder the same way ``test_generate_flow_route.py`` and
``test_knowledge_api.py`` do) the upload route's success path and its PDF
extraction branch — hermetically, without a live DB or network call.
"""

from __future__ import annotations

import contextlib
import io

from koel.voicebot.api import knowledge as knowledge_api
from koel.voicebot.api.knowledge import _extract_text


def test_extract_text_utf8_and_latin1_fallback():
    assert _extract_text("kb.txt", "नमस्ते hello".encode()) == "नमस्ते hello"
    # invalid UTF-8 must not crash — falls back to latin-1.
    assert "abc" in _extract_text("kb.md", b"abc\xff\xfe")


def _minimal_pdf(text: bytes) -> bytes:
    """Hand-rolled, minimally-valid single-page PDF whose content stream draws
    ``text`` — enough for pypdf to parse and extract_text() to recover it, without
    pulling in a PDF-authoring dependency just for a test fixture."""
    content = b"BT /F1 24 Tf 100 700 Td (%s) Tj ET" % text
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 612 792] /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
    ]
    buf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(buf))
        buf += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_offset = len(buf)
    n = len(objs) + 1
    buf += f"xref\n0 {n}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets[1:]:
        buf += f"{off:010d} 00000 n \n".encode()
    buf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (n, xref_offset)
    return bytes(buf)


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self):  # noqa: ANN201
        return self

    def all(self):  # noqa: ANN201
        return self._rows


class _FakeSession:
    """Same hermetic-DB pattern as test_knowledge_api.py / test_generate_flow_route.py."""

    def __init__(self) -> None:
        self.added: list = []
        self.flush_count = 0

    async def execute(self, statement, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
        return _FakeResult([])

    def add(self, row) -> None:  # noqa: ANN001
        if getattr(row, "id", None) is None:
            row.id = f"chunk_{len(self.added) + 1}"
        self.added.append(row)

    async def flush(self) -> None:
        self.flush_count += 1


def _patch_db(monkeypatch) -> _FakeSession:
    session = _FakeSession()

    @contextlib.asynccontextmanager
    async def _fake_scope():
        yield session

    monkeypatch.setattr(knowledge_api, "session_scope", _fake_scope)
    return session


class _FakeEmbedder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def embed(self, text: str, *, input_type: str = "query") -> list[float]:
        self.calls.append((text, input_type))
        return [0.1, 0.2, 0.3]


def _patch_embedder(monkeypatch) -> _FakeEmbedder:
    embedder = _FakeEmbedder()
    monkeypatch.setattr(knowledge_api, "_get_embedder", lambda: embedder)
    return embedder


def _headers(access_headers: dict) -> dict:
    # Multipart upload: send ONLY Authorization (browser/httpx sets the boundary).
    return {"Authorization": access_headers["Authorization"]}


async def test_upload_empty_file_rejected(app_client, access_headers, test_db):
    client, _ = app_client
    r = await client.post(
        "/v1/knowledge-files",
        headers=_headers(access_headers),
        files={"file": ("empty.txt", b"", "text/plain")},
        data={"rag_index": "cc_hdfc", "use_case": "credit_card"},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "empty_file"


async def test_upload_textless_file_rejected(app_client, access_headers, test_db):
    # Whitespace-only file → nothing to embed → 400 before any DB write.
    client, _ = app_client
    r = await client.post(
        "/v1/knowledge-files",
        headers=_headers(access_headers),
        files={"file": ("blank.txt", b"   \n  ", "text/plain")},
        data={"rag_index": "cc_hdfc", "use_case": "credit_card"},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "empty_content"


async def test_upload_requires_auth(app_client, test_db):
    client, _ = app_client
    r = await client.post(
        "/v1/knowledge-files",
        files={"file": ("kb.txt", b"hello", "text/plain")},
        data={"rag_index": "cc_hdfc", "use_case": "credit_card"},
    )
    assert r.status_code == 401


async def test_upload_file_too_large_is_413(app_client, access_headers, test_db):
    # One byte over the 2 MB ceiling → rejected before any text extraction/DB write.
    oversized = b"a" * (knowledge_api._MAX_UPLOAD_BYTES + 1)
    client, _ = app_client
    r = await client.post(
        "/v1/knowledge-files",
        headers=_headers(access_headers),
        files={"file": ("big.txt", oversized, "text/plain")},
        data={"rag_index": "cc_hdfc", "use_case": "credit_card"},
    )
    assert r.status_code == 413
    body = r.json()
    assert body["code"] == "file_too_large"
    assert "2000 KB" in body["message"]


async def test_upload_pdf_with_no_extractable_text_is_400(app_client, access_headers, test_db):
    # A real, parseable, but blank PDF page: extract_text() legitimately returns
    # "" for every page, so this exercises the PDF-parsing branch itself (not
    # just the generic empty-content guard the .txt tests above hit).
    import pypdf

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)

    client, _ = app_client
    r = await client.post(
        "/v1/knowledge-files",
        headers=_headers(access_headers),
        files={"file": ("blank.pdf", buf.getvalue(), "application/pdf")},
        data={"rag_index": "cc_hdfc", "use_case": "credit_card"},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "empty_content"


async def test_upload_pdf_extracts_text_and_creates_chunks(
    app_client, monkeypatch, access_headers, test_db
):
    # Success path: real pypdf extraction feeding a real (faked-DB, faked-
    # embedder) chunk creation — hermetic, but exercises the actual PDF→text→
    # chunk_text→embed→persist pipeline end to end.
    client, _ = app_client
    session = _patch_db(monkeypatch)
    embedder = _patch_embedder(monkeypatch)

    pdf_bytes = _minimal_pdf(b"Hello World")
    r = await client.post(
        "/v1/knowledge-files",
        headers=_headers(access_headers),
        files={"file": ("kb.pdf", pdf_bytes, "application/pdf")},
        data={"rag_index": "cc_hdfc", "use_case": "credit_card", "language": "en"},
    )
    assert r.status_code == 201
    body = r.json()
    assert len(body) == 1
    assert body[0]["title"] == "kb.pdf"
    assert body[0]["content"] == "Hello World"
    assert body[0]["rag_index"] == "cc_hdfc"
    assert body[0]["language"] == "en"
    assert session.added[0].org_id is None  # access_headers' demo token is unscoped
    assert session.flush_count == 1
    assert embedder.calls == [("kb.pdf Hello World", "passage")]


async def test_upload_text_file_creates_chunk_scoped_to_caller_org(app_client, monkeypatch, test_db):
    # Plain-text success path, with an org-scoped caller — the stored row must
    # carry that org (AF tenant scoping), same as the JSON create-chunk route.
    from koel.common.auth import issue_token

    client, _ = app_client
    session = _patch_db(monkeypatch)
    _patch_embedder(monkeypatch)

    headers = {"Authorization": f"Bearer {issue_token('op_1', kind='access', org_id='org_a')}"}
    r = await client.post(
        "/v1/knowledge-files",
        headers=headers,
        files={"file": ("notes.txt", b"No annual fee for the first year.", "text/plain")},
        data={"rag_index": "cc_hdfc", "use_case": "credit_card"},
    )
    assert r.status_code == 201
    assert session.added[0].org_id == "org_a"
    assert session.added[0].rag_index == "cc_hdfc"
