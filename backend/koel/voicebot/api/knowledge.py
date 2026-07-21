"""Knowledge-base admin API (X, PRD §06 RAG).

L2 CRUD over ``knowledge_chunks`` so operators can manage per-bot KB content
without a redeploy or a DB console. Content is embedded on write with the
configured embedder (same one NLP_svc retrieves with), so a newly-created chunk
is immediately retrievable. Tenant-scoped (AF).
"""

from __future__ import annotations

import io

from fastapi import APIRouter, File, Form, Query, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from koel.common.auth import CurrentUser
from koel.common.errors import ApiError
from koel.db.models import KnowledgeChunk, UseCase
from koel.db.session import session_scope
from koel.nlp.chunking import chunk_text
from koel.nlp.config import get_nlp_settings
from koel.nlp.flow_gen import generate_flow
from koel.nlp.rag import Embedder, build_embedder

router = APIRouter(prefix="/v1", tags=["knowledge"])

# One embedder for the process (holds an HTTP pool); built lazily so importing
# the app never triggers a network client.
_embedder: Embedder | None = None


def _get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = build_embedder(get_nlp_settings())
    return _embedder


class ChunkIn(BaseModel):
    rag_index: str = Field(max_length=64)
    use_case: UseCase
    language: str = Field(default="hinglish", max_length=16)
    title: str = Field(max_length=200)
    # Bounded so a single chunk can't trigger an unbounded embedding call (cost /
    # memory / provider-limit DoS). Long docs should be chunked (auto_chunk) rather
    # than posted as one huge blob; 100k chars is a generous ceiling for one doc.
    content: str = Field(min_length=1, max_length=100_000)
    # If true, split long content into multiple ~512-token passages before embedding.
    chunk: bool = False


class ChunkOut(BaseModel):
    id: str
    rag_index: str
    use_case: UseCase
    language: str
    title: str
    content: str


class IndexSummary(BaseModel):
    rag_index: str
    chunk_count: int


def _to_out(row: KnowledgeChunk) -> ChunkOut:
    return ChunkOut(
        id=row.id,
        rag_index=row.rag_index,
        use_case=row.use_case,
        language=row.language,
        title=row.title,
        content=row.content,
    )


@router.get("/knowledge-indexes", response_model=list[IndexSummary])
async def list_indexes(user: CurrentUser) -> list[IndexSummary]:
    stmt = select(KnowledgeChunk.rag_index, func.count().label("n")).group_by(
        KnowledgeChunk.rag_index
    )
    if user.org_id is not None:
        stmt = stmt.where(KnowledgeChunk.org_id == user.org_id)
    async with session_scope() as db:
        rows = (await db.execute(stmt)).all()
    return [IndexSummary(rag_index=idx, chunk_count=n) for idx, n in rows]


@router.get("/knowledge-chunks", response_model=list[ChunkOut])
async def list_chunks(
    user: CurrentUser,
    rag_index: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
) -> list[ChunkOut]:
    stmt = select(KnowledgeChunk).order_by(KnowledgeChunk.created_at.desc()).limit(limit)
    if rag_index:
        stmt = stmt.where(KnowledgeChunk.rag_index == rag_index)
    if user.org_id is not None:
        stmt = stmt.where(KnowledgeChunk.org_id == user.org_id)
    async with session_scope() as db:
        rows = (await db.execute(stmt)).scalars().all()
    return [_to_out(r) for r in rows]


@router.post("/knowledge-chunks", response_model=list[ChunkOut], status_code=status.HTTP_201_CREATED)
async def create_chunks(payload: ChunkIn, user: CurrentUser) -> list[ChunkOut]:
    """Create one chunk, or several if ``chunk=true`` splits long content."""
    embedder = _get_embedder()
    bodies = chunk_text(payload.content) if payload.chunk else [payload.content]
    if not bodies:
        raise ApiError(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="empty_content",
            message="content produced no chunks.",
        )
    created: list[ChunkOut] = []
    async with session_scope() as db:
        for i, body in enumerate(bodies):
            title = payload.title if len(bodies) == 1 else f"{payload.title} ({i + 1}/{len(bodies)})"
            row = KnowledgeChunk(
                org_id=user.org_id,
                rag_index=payload.rag_index,
                use_case=payload.use_case,
                language=payload.language,
                title=title,
                content=body,
                embedding=await embedder.embed(f"{title} {body}", input_type="passage"),
            )
            db.add(row)
            await db.flush()
            created.append(_to_out(row))
    return created


_MAX_UPLOAD_BYTES = 2_000_000  # 2 MB — plenty for a KB doc, bounds memory + embed cost


def _extract_text(filename: str, data: bytes) -> str:
    """Pull plain text from an uploaded file. Text formats decode directly; PDFs
    use pypdf when available (degrades to a clear 415 if not installed)."""
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        try:
            import pypdf
        except ImportError as exc:  # pragma: no cover - dep present in the image
            raise ApiError(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                code="pdf_unsupported",
                message="PDF parsing is not available on this server; upload text instead.",
            ) from exc
        reader = pypdf.PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="ignore")


@router.post(
    "/knowledge-files", response_model=list[ChunkOut], status_code=status.HTTP_201_CREATED
)
async def upload_knowledge_file(
    user: CurrentUser,
    file: UploadFile = File(...),
    rag_index: str = Form(...),
    use_case: UseCase = Form(...),
    language: str = Form("hinglish"),
) -> list[ChunkOut]:
    """Upload a text or PDF file → extract, split into passages, embed, and store as
    knowledge chunks under ``rag_index`` (X — file-based KB ingestion, PRD §06)."""
    data = await file.read()
    if not data:
        raise ApiError(
            status_code=status.HTTP_400_BAD_REQUEST, code="empty_file", message="Uploaded file is empty."
        )
    if len(data) > _MAX_UPLOAD_BYTES:
        raise ApiError(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            code="file_too_large",
            message=f"File exceeds the {_MAX_UPLOAD_BYTES // 1000} KB limit.",
        )
    text = _extract_text(file.filename or "upload", data).strip()
    bodies = chunk_text(text)
    if not bodies:
        raise ApiError(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="empty_content",
            message="No extractable text found in the file.",
        )
    fname = file.filename or "upload"
    embedder = _get_embedder()
    created: list[ChunkOut] = []
    async with session_scope() as db:
        for i, body in enumerate(bodies):
            title = fname if len(bodies) == 1 else f"{fname} ({i + 1}/{len(bodies)})"
            row = KnowledgeChunk(
                org_id=user.org_id,
                rag_index=rag_index,
                use_case=use_case,
                language=language,
                title=title,
                content=body,
                embedding=await embedder.embed(f"{title} {body}", input_type="passage"),
            )
            db.add(row)
            await db.flush()
            created.append(_to_out(row))
    return created


@router.delete("/knowledge-chunks/{chunk_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chunk(chunk_id: str, user: CurrentUser) -> None:
    async with session_scope() as db:
        row = await db.get(KnowledgeChunk, chunk_id)
        if row is None or (user.org_id is not None and row.org_id != user.org_id):
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code="chunk_not_found",
                message=f"No knowledge chunk with id {chunk_id}.",
            )
        await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.id == chunk_id))


class GenerateFlowIn(BaseModel):
    rag_index: str = Field(max_length=64)
    use_case: str | None = Field(default=None, max_length=64)
    language: str = Field(default="en", max_length=16)


class GenerateFlowOut(BaseModel):
    flow: dict  # {start, nodes} — the FlowEditor's Flow shape (auto-layout added FE-side)
    node_count: int
    source_chunks: int


@router.post("/knowledge/generate-flow", response_model=GenerateFlowOut)
async def generate_flow_route(payload: GenerateFlowIn, user: CurrentUser) -> GenerateFlowOut:
    """Auto node-builder (6.6): draft a conversation flow from this index's RAG
    content via the LLM. Org-scoped; the operator refines the result in the editor.
    404 when the index has no content; 503 when the LLM isn't configured."""
    stmt = (
        select(KnowledgeChunk)
        .where(KnowledgeChunk.rag_index == payload.rag_index)
        .order_by(KnowledgeChunk.created_at.desc())
        .limit(20)
    )
    if user.org_id is not None:
        stmt = stmt.where(KnowledgeChunk.org_id == user.org_id)
    async with session_scope() as db:
        rows = (await db.execute(stmt)).scalars().all()
    if not rows:
        raise ApiError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="no_kb_content",
            message=f"No knowledge base content for index '{payload.rag_index}'.",
        )
    chunks = [{"title": r.title, "content": r.content} for r in rows]
    use_case = payload.use_case or rows[0].use_case.value
    flow = await generate_flow(
        chunks=chunks, use_case=use_case, language=payload.language, settings=get_nlp_settings()
    )
    if flow is None:
        raise ApiError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="generation_unavailable",
            message="Flow generation is unavailable (LLM not configured or the draft "
            "failed to parse). Build nodes manually in the editor.",
        )
    return GenerateFlowOut(flow=flow, node_count=len(flow["nodes"]), source_chunks=len(chunks))


async def aclose_embedder() -> None:
    """Release the module embedder's HTTP pool (called from the app lifespan)."""
    global _embedder
    if _embedder is not None:
        aclose = getattr(_embedder, "aclose", None)
        if aclose is not None:
            await aclose()
        _embedder = None
