"""RAG retrieval over the pgvector knowledge base.

Embeds the query, runs a cosine-distance nearest-neighbour search against
``knowledge_chunks`` scoped to the bot's ``rag_index``. The embedder is an
interface: a deterministic hash stub for CI, real NVIDIA NIM multilingual
embeddings for production (Groq has no embedding model), or Bedrock Titan.
"""

from __future__ import annotations

import hashlib
import logging
import math
from abc import ABC, abstractmethod

import httpx
from sqlalchemy import select

from koel.db.models import EMBEDDING_DIM, KnowledgeChunk
from koel.db.session import session_scope
from koel.nlp.schemas import RetrievedChunk

logger = logging.getLogger(__name__)


class Embedder(ABC):
    @abstractmethod
    async def embed(self, text: str, *, input_type: str = "query") -> list[float]:
        """Embed text. ``input_type`` is "query" or "passage" — asymmetric models
        (e.g. nv-embedqa) embed queries and documents differently."""
        ...


class HashEmbedder(Embedder):
    """Deterministic, dependency-free embedder for local demo/CI.

    Hashes tokens into a fixed-dimension bag-of-words vector and L2-normalises.
    Semantically weak (no cross-lingual understanding) — use NVIDIA/Bedrock in prod.
    """

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self.dim = dim

    async def embed(self, text: str, *, input_type: str = "query") -> list[float]:
        vec = [0.0] * self.dim
        for token in text.lower().split():
            h = int(hashlib.md5(token.encode()).hexdigest(), 16)
            vec[h % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class NvidiaEmbedder(Embedder):
    """Live multilingual embeddings via NVIDIA NIM (OpenAI-compatible /embeddings).

    nv-embedqa-e5-v5 is 1024-d (matches EMBEDDING_DIM) and multilingual, so Hindi/
    Hinglish queries retrieve semantically. Degrades to the hash stub on failure so
    retrieval never crashes a turn (RAG is best-effort).
    """

    def __init__(self, *, api_key: str, model: str, base_url: str, timeout_s: float = 20.0) -> None:
        self._model = model
        self._url = f"{base_url.rstrip('/')}/embeddings"
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._client = httpx.AsyncClient(timeout=timeout_s)
        self._fallback = HashEmbedder()
        self._warned = False

    async def embed(self, text: str, *, input_type: str = "query") -> list[float]:
        try:
            resp = await self._client.post(
                self._url,
                headers=self._headers,
                json={
                    "input": [text],
                    "model": self._model,
                    "input_type": input_type,  # query | passage
                    "encoding_format": "float",
                },
            )
            resp.raise_for_status()
            return resp.json()["data"][0]["embedding"]
        except Exception:  # noqa: BLE001 — RAG is best-effort; degrade to the stub
            if not self._warned:
                self._warned = True
                logger.warning("nvidia_embed_failed; falling back to hash embedder", exc_info=True)
            return await self._fallback.embed(text, input_type=input_type)

    async def aclose(self) -> None:
        await self._client.aclose()


class BedrockTitanEmbedder(Embedder):  # pragma: no cover - production stub
    async def embed(self, text: str, *, input_type: str = "query") -> list[float]:
        raise NotImplementedError("Invoke Bedrock Titan Embeddings v2 (ap-south-1) here.")


def build_embedder(settings) -> Embedder:  # noqa: ANN001 — NlpSettings (avoid import cycle)
    """Select the embedder from config. NVIDIA if enabled + keyed, else stub."""
    kind = (getattr(settings, "nlp_embedder", "hash") or "hash").lower()
    if kind == "nvidia" and settings.nvidia_api_key:
        return NvidiaEmbedder(
            api_key=settings.nvidia_api_key,
            model=settings.nvidia_embed_model,
            base_url=settings.nvidia_embed_base_url,
            timeout_s=settings.nvidia_embed_timeout_s,
        )
    if kind == "bedrock":
        return BedrockTitanEmbedder()
    return HashEmbedder()


async def retrieve(
    query: str,
    *,
    rag_index: str,
    embedder: Embedder,
    top_k: int = 3,
    score_threshold: float = 0.0,
) -> list[RetrievedChunk]:
    embedding = await embedder.embed(query, input_type="query")
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

    # Drop weakly-relevant chunks so the LLM isn't "grounded" on noise (PRD §12).
    out: list[RetrievedChunk] = []
    for chunk, distance in rows:
        score = 1.0 - float(distance)
        if score >= score_threshold:
            out.append(RetrievedChunk(title=chunk.title, content=chunk.content, score=score))
    return out
