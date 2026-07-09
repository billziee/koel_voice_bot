"""RAG embedder units + KB admin embedder lifecycle (DB-free paths)."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import httpx

from koel.db.models import EMBEDDING_DIM
from koel.nlp.rag import FastEmbedEmbedder, HashEmbedder, NvidiaEmbedder, build_embedder


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


def _install_fake_fastembed(monkeypatch, *, dim: int) -> None:
    """Inject a fake ``fastembed`` module so FastEmbedEmbedder (which lazy-imports
    it) works without the optional dependency or a model download."""
    fake = types.ModuleType("fastembed")

    class _TextEmbedding:
        def __init__(self, model_name: str) -> None:
            self.model_name = model_name

        def embed(self, texts):  # noqa: ANN001, ANN202 — mirrors fastembed's API
            return [[0.1] * dim for _ in texts]

    fake.TextEmbedding = _TextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", fake)


async def test_fastembed_embedder_returns_model_vector(monkeypatch):
    _install_fake_fastembed(monkeypatch, dim=EMBEDDING_DIM)
    e = FastEmbedEmbedder(model="BAAI/bge-m3")
    out = await e.embed("annual fee waived", input_type="query")
    assert len(out) == EMBEDDING_DIM


async def test_fastembed_embedder_falls_back_on_bad_shape(monkeypatch):
    _install_fake_fastembed(monkeypatch, dim=8)  # wrong width
    e = FastEmbedEmbedder(model="BAAI/bge-m3")
    out = await e.embed("x", input_type="passage")
    assert len(out) == EMBEDDING_DIM  # degraded to the hash embedder, never wrong-dim


def test_build_embedder_selects_fastembed(monkeypatch):
    _install_fake_fastembed(monkeypatch, dim=EMBEDDING_DIM)
    settings = SimpleNamespace(nlp_embedder="fastembed", fastembed_model="BAAI/bge-m3")
    assert isinstance(build_embedder(settings), FastEmbedEmbedder)


async def test_kb_embedder_lifecycle():
    from koel.voicebot.api import knowledge

    emb = knowledge._get_embedder()
    assert emb is knowledge._get_embedder()  # memoised singleton
    await knowledge.aclose_embedder()
    # After close it rebuilds cleanly on next use.
    assert knowledge._get_embedder() is not None
    await knowledge.aclose_embedder()
