"""File-upload KB ingestion (X): text/PDF extraction + endpoint validation.

The happy path writes a pgvector KnowledgeChunk (not creatable on the SQLite test
DB — conftest excludes it), so it's verified live; here we cover the pure text
extraction and the pre-DB validation the endpoint enforces.
"""

from __future__ import annotations

from koel.voicebot.api.knowledge import _extract_text


def test_extract_text_utf8_and_latin1_fallback():
    assert _extract_text("kb.txt", "नमस्ते hello".encode()) == "नमस्ते hello"
    # invalid UTF-8 must not crash — falls back to latin-1.
    assert "abc" in _extract_text("kb.md", b"abc\xff\xfe")


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
