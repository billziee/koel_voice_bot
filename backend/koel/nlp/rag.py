"""RAG retrieval over the pgvector knowledge base.

Embeds the query, runs a cosine-distance nearest-neighbour search against
``knowledge_chunks`` scoped to the bot's ``rag_index``. The embedder is an
interface: a deterministic hash stub for CI, real NVIDIA NIM multilingual
embeddings for production (Groq has no embedding model), or Bedrock Titan.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
from abc import ABC, abstractmethod

from sqlalchemy import select, text

from koel.common.config import get_common_settings
from koel.common.http import make_async_client
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
        self._client = make_async_client(timeout_s=timeout_s)
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
            vec = resp.json()["data"][0]["embedding"]
            # Guard the dimension (plan V): a wrong-width vector would break the
            # pgvector query, so treat it as a failure and degrade to the stub.
            if len(vec) != EMBEDDING_DIM:
                raise ValueError(f"embedding dim {len(vec)} != {EMBEDDING_DIM}")
            return vec
        except Exception:  # noqa: BLE001 — RAG is best-effort; degrade to the stub
            if not self._warned:
                self._warned = True
                logger.warning("nvidia_embed_failed; falling back to hash embedder", exc_info=True)
            return await self._fallback.embed(text, input_type=input_type)

    async def aclose(self) -> None:
        await self._client.aclose()


class FastEmbedEmbedder(Embedder):
    """Local ONNX CPU embeddings via fastembed (roadmap O-4).

    Runs the embedder in-process instead of a ~250ms cross-ocean round-trip to
    NVIDIA on every RAG turn (~10-40ms local CPU). BAAI/bge-m3 is 1024-d and
    multilingual (Hindi/Hinglish), so it matches ``EMBEDDING_DIM`` — no schema
    migration, only a one-off re-embed of existing KB chunks (query and document
    vectors must share a model — see ``scripts/reembed_kb.py``). bge-m3 is
    symmetric, so ``input_type`` doesn't change the vector.

    fastembed is an OPTIONAL dependency (``pip install 'koel[local-embed]'``);
    it's imported lazily so the default deploy is unaffected. The model is
    downloaded/loaded once at construction. Degrades to the hash stub on any
    failure so a bad model load never crashes a turn (RAG is best-effort).
    """

    def __init__(self, *, model: str) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise RuntimeError(
                "nlp_embedder='fastembed' requires the optional dependency: "
                "pip install 'koel[local-embed]'"
            ) from exc
        self._model = TextEmbedding(model_name=model)
        self._fallback = HashEmbedder()
        self._warned = False

    async def embed(self, text: str, *, input_type: str = "query") -> list[float]:
        try:
            # fastembed is synchronous + CPU-bound; keep it off the event loop.
            vecs = await asyncio.to_thread(lambda: list(self._model.embed([text])))
            vec = [float(x) for x in vecs[0]]
            if len(vec) != EMBEDDING_DIM:
                raise ValueError(f"embedding dim {len(vec)} != {EMBEDDING_DIM}")
            return vec
        except Exception:  # noqa: BLE001 — RAG is best-effort; degrade to the stub
            if not self._warned:
                self._warned = True
                logger.warning("fastembed_failed; falling back to hash embedder", exc_info=True)
            return await self._fallback.embed(text, input_type=input_type)


class BedrockTitanEmbedder(Embedder):  # pragma: no cover - production stub
    async def embed(self, text: str, *, input_type: str = "query") -> list[float]:
        raise NotImplementedError("Invoke Bedrock Titan Embeddings v2 (ap-south-1) here.")


# Embedders that are selectable via config but not yet implemented. Selecting
# one is a boot-time misconfiguration, not a runtime condition — build_embedder
# rejects it at startup (Item Zero in docs/FUTURE_PROCEEDINGS.md) so the failure
# surfaces immediately instead of on the first RAG turn mid-call. When one is
# implemented (roadmap 6.8 / O-7 Track B), remove it from this set and route to
# the real provider above.
_UNIMPLEMENTED_EMBEDDERS = {"bedrock": "BedrockTitanEmbedder"}


def build_embedder(settings) -> Embedder:  # noqa: ANN001 — NlpSettings (avoid import cycle)
    """Select the embedder from config. NVIDIA if enabled + keyed, else stub.

    Raises NotImplementedError at startup if config selects a known-unimplemented
    provider, so a misconfiguration fails loudly at boot rather than crashing the
    first real RAG turn.
    """
    kind = (getattr(settings, "nlp_embedder", "hash") or "hash").lower()
    if kind in _UNIMPLEMENTED_EMBEDDERS:
        raise NotImplementedError(
            f"nlp_embedder={kind!r} selects {_UNIMPLEMENTED_EMBEDDERS[kind]}, which is not "
            f"implemented yet (see koel/nlp/rag.py). Set KOEL_NLP_EMBEDDER to 'nvidia' "
            f"(with KOEL_NVIDIA_API_KEY) or 'hash' (deterministic dev/CI stub)."
        )
    if kind == "fastembed":
        return FastEmbedEmbedder(model=settings.fastembed_model)
    if kind == "nvidia" and settings.nvidia_api_key:
        return NvidiaEmbedder(
            api_key=settings.nvidia_api_key,
            model=settings.nvidia_embed_model,
            base_url=settings.nvidia_embed_base_url,
            timeout_s=settings.nvidia_embed_timeout_s,
        )
    return HashEmbedder()


async def retrieve(
    query: str,
    *,
    rag_index: str,
    embedder: Embedder,
    top_k: int = 3,
    score_threshold: float = 0.0,
    org_id: str | None = None,
) -> list[RetrievedChunk]:
    embedding = await embedder.embed(query, input_type="query")
    ef_search = get_common_settings().pgvector_hnsw_ef_search
    async with session_scope() as db:
        # HNSW recall/latency knob (O-10): raise ef_search to trade query latency
        # for recall. SET LOCAL is transaction-scoped, so it applies only to this
        # session_scope() transaction and reverts on commit/rollback. No-op at 0
        # (leave the server default) and best-effort — a failure here must never
        # break retrieval (RAG is degrade-safe). int() hardens the interpolation
        # (SET rejects bind params) against a bad config value.
        if ef_search > 0:
            try:
                await db.execute(text(f"SET LOCAL hnsw.ef_search = {int(ef_search)}"))
            except Exception:  # noqa: BLE001 — best-effort; fall back to server default
                logger.debug("hnsw_ef_search_set_local_failed", exc_info=True)
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
        # Tenant isolation (AF): a scoped bot only retrieves its org's chunks, so
        # two orgs sharing a rag_index name can't read each other's KB.
        if org_id is not None:
            stmt = stmt.where(KnowledgeChunk.org_id == org_id)
        rows = (await db.execute(stmt)).all()

    # Drop weakly-relevant chunks so the LLM isn't "grounded" on noise (PRD §12).
    out: list[RetrievedChunk] = []
    for chunk, distance in rows:
        score = 1.0 - float(distance)
        if score >= score_threshold:
            out.append(RetrievedChunk(title=chunk.title, content=chunk.content, score=score))
    return out
