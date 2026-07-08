"""Tests for the RAG embedder factory + fallback (offline)."""

from __future__ import annotations

import base64  # noqa: F401 — kept parallel with sibling test imports
from types import SimpleNamespace

import pytest

from koel.db.models import EMBEDDING_DIM
from koel.nlp.rag import HashEmbedder, NvidiaEmbedder, build_embedder


def _settings(**over):
    base = dict(
        nlp_embedder="hash",
        nvidia_api_key=None,
        nvidia_embed_model="nvidia/nv-embedqa-e5-v5",
        nvidia_embed_base_url="https://integrate.api.nvidia.com/v1",
        nvidia_embed_timeout_s=5.0,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_factory_selects_by_config():
    assert isinstance(build_embedder(_settings(nlp_embedder="hash")), HashEmbedder)
    assert isinstance(
        build_embedder(_settings(nlp_embedder="nvidia", nvidia_api_key="k")), NvidiaEmbedder
    )
    # nvidia selected but no key → safe fallback to hash.
    assert isinstance(build_embedder(_settings(nlp_embedder="nvidia")), HashEmbedder)


def test_factory_rejects_unimplemented_provider_at_startup():
    # Item Zero (FUTURE_PROCEEDINGS): selecting a provider that isn't implemented
    # must fail loudly at build time (startup) — not silently construct a stub
    # whose embed() raises mid-call on the first real user.
    with pytest.raises(NotImplementedError, match="bedrock"):
        build_embedder(_settings(nlp_embedder="bedrock"))

    # Case-insensitive: the factory lower-cases the config value.
    with pytest.raises(NotImplementedError, match="bedrock"):
        build_embedder(_settings(nlp_embedder="BEDROCK"))


async def test_hash_embedder_shape_and_input_type():
    e = HashEmbedder()
    v = await e.embed("aapke paas offer hai", input_type="passage")
    assert len(v) == EMBEDDING_DIM
    assert abs(sum(x * x for x in v) - 1.0) < 1e-6  # L2-normalised


async def test_nvidia_embedder_falls_back_to_hash_on_unreachable_host():
    e = NvidiaEmbedder(
        api_key="x", model="nvidia/nv-embedqa-e5-v5", base_url="http://127.0.0.1:1/v1", timeout_s=2
    )
    v = await e.embed("namaste", input_type="query")
    assert len(v) == EMBEDDING_DIM  # degraded to hash, never raised
    await e.aclose()
