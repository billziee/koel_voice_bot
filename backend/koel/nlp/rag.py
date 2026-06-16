"""RAG retrieval over the pgvector knowledge base.

Embeds the query, runs a cosine-distance nearest-neighbour search against
``knowledge_chunks`` scoped to the bot's ``rag_index``. The embedder is an
interface so Bedrock Titan embeddings drop in behind the demo hash-embedder.
"""

from __future__ import annotations

import hashlib
import math
from abc import ABC, abstractmethod

from sqlalchemy import select

from koel.db.models import EMBEDDING_DIM, KnowledgeChunk
from koel.db.session import session_scope
from koel.nlp.schemas import RetrievedChunk


class Embedder(ABC):
    @abstractmethod
    async def embed(self, text: str) -> list[float]: ...


class HashEmbedder(Embedder):
    """Deterministic, dependency-free embedder for local demo/CI.

    Hashes tokens into a fixed-dimension bag-of-words vector and L2-normalises.
    Good enough for the demo to retrieve relevant seeded chunks; replace with
    Bedrock Titan in production.
    """

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self.dim = dim

    async def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in text.lower().split():
            h = int(hashlib.md5(token.encode()).hexdigest(), 16)
            vec[h % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class BedrockTitanEmbedder(Embedder):  # pragma: no cover - production stub
    async def embed(self, text: str) -> list[float]:
        raise NotImplementedError("Invoke Bedrock Titan Embeddings v2 here.")


async def retrieve(
    query: str,
    *,
    rag_index: str,
    embedder: Embedder,
    top_k: int = 3,
) -> list[RetrievedChunk]:
    embedding = await embedder.embed(query)
    async with session_scope() as db:
        # pgvector cosine distance operator (<=>); smaller distance == closer.
        stmt = (
            select(
                KnowledgeChunk,
                KnowledgeChunk.embedding.cosine_distance(embedding).label("distance"),
            )
            .where(KnowledgeChunk.rag_index == rag_index)
            .order_by("distance")
            .limit(top_k)
        )
        rows = (await db.execute(stmt)).all()

    return [
        RetrievedChunk(title=chunk.title, content=chunk.content, score=1.0 - float(distance))
        for chunk, distance in rows
    ]
