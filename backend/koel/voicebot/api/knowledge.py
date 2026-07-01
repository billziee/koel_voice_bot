"""Knowledge-base admin API (X, PRD §06 RAG).

L2 CRUD over ``knowledge_chunks`` so operators can manage per-bot KB content
without a redeploy or a DB console. Content is embedded on write with the
configured embedder (same one NLP_svc retrieves with), so a newly-created chunk
is immediately retrievable. Tenant-scoped (AF).
"""

from __future__ import annotations

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from koel.common.auth import CurrentUser
from koel.common.errors import ApiError
from koel.db.models import KnowledgeChunk, UseCase
from koel.db.session import session_scope
from koel.nlp.chunking import chunk_text
from koel.nlp.config import get_nlp_settings
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
    content: str = Field(min_length=1)
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


async def aclose_embedder() -> None:
    """Release the module embedder's HTTP pool (called from the app lifespan)."""
    global _embedder
    if _embedder is not None:
        aclose = getattr(_embedder, "aclose", None)
        if aclose is not None:
            await aclose()
        _embedder = None
