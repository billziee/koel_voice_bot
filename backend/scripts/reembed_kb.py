#!/usr/bin/env python
"""Re-embed every knowledge_chunks row with the currently-configured embedder (O-4).

Query and document vectors MUST come from the same model, so after switching
``KOEL_NLP_EMBEDDER`` (e.g. nvidia → fastembed) the stored KB vectors are stale
and retrieval degrades to noise until they're regenerated. Run this once after
the switch:

    KOEL_NLP_EMBEDDER=fastembed python -m scripts.reembed_kb

The pgvector HNSW index rebuilds automatically as rows are updated. Idempotent —
re-running just re-embeds with the same model. EMBEDDING_DIM is unchanged
(bge-m3 and nv-embedqa are both 1024-d), so this is data-only, no schema change.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from koel.db.models import KnowledgeChunk
from koel.db.session import session_scope
from koel.nlp.config import NlpSettings
from koel.nlp.rag import build_embedder


async def reembed_all() -> int:
    """Re-embed all KB chunks in place; returns the number updated."""
    embedder = build_embedder(NlpSettings())
    async with session_scope() as db:  # commits on success
        chunks = (await db.execute(select(KnowledgeChunk))).scalars().all()
        for chunk in chunks:
            # Same text composition the ingestion path uses (title + content),
            # embedded as a passage so it matches the query-side asymmetry.
            chunk.embedding = await embedder.embed(
                f"{chunk.title} {chunk.content}", input_type="passage"
            )
        return len(chunks)


async def main() -> None:
    n = await reembed_all()
    print(f"re-embedded {n} knowledge chunk(s) with the configured embedder")


if __name__ == "__main__":
    asyncio.run(main())
