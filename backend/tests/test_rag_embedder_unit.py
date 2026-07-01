"""RAG embedder units + KB admin embedder lifecycle (DB-free paths)."""

from __future__ import annotations

import httpx

from koel.db.models import EMBEDDING_DIM
from koel.nlp.rag import HashEmbedder, NvidiaEmbedder


async def test_hash_embedder_is_deterministic_and_normalised():
    e = HashEmbedder()
    a = await e.embed("annual fee waived", input_type="query")
    b = await e.embed("annual fee waived", input_type="query")
    assert a == b and len(a) == EMBEDDING_DIM
    assert abs(sum(x * x for x in a) - 1.0) < 1e-6


async def test_nvidia_embedder_parses_api_response(monkeypatch):
    e = NvidiaEmbedder(api_key="k", model="nvidia/nv-embedqa-e5-v5",
                       base_url="https://x/v1", timeout_s=5)
    vec = [0.1] * EMBEDDING_DIM

    async def ok(*a, **k):
        return httpx.Response(200, json={"data": [{"embedding": vec}]},
                              request=httpx.Request("POST", "http://x"))

    monkeypatch.setattr(e._client, "post", ok)
    out = await e.embed("namaste", input_type="passage")
    assert len(out) == EMBEDDING_DIM
    await e.aclose()


async def test_nvidia_embedder_falls_back_on_bad_shape(monkeypatch):
    e = NvidiaEmbedder(api_key="k", model="m", base_url="https://x/v1", timeout_s=5)

    async def bad(*a, **k):
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}]},  # wrong dim
                              request=httpx.Request("POST", "http://x"))

    monkeypatch.setattr(e._client, "post", bad)
    out = await e.embed("x", input_type="query")
    assert len(out) == EMBEDDING_DIM  # degraded to the hash embedder, never wrong-dim
    await e.aclose()


async def test_kb_embedder_lifecycle():
    from koel.voicebot.api import knowledge

    emb = knowledge._get_embedder()
    assert emb is knowledge._get_embedder()  # memoised singleton
    await knowledge.aclose_embedder()
    # After close it rebuilds cleanly on next use.
    assert knowledge._get_embedder() is not None
    await knowledge.aclose_embedder()
