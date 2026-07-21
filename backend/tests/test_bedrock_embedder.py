"""Tests for BedrockTitanEmbedder (AWS Bedrock Titan Embeddings V2, offline).

Mirrors tests/test_rag_embedder_unit.py's NvidiaEmbedder tests: boto3's
``invoke_model`` is monkeypatched on the already-constructed client (no real
AWS credentials or network access required), matching how the NVIDIA tests
monkeypatch ``e._client.post``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from koel.db.models import EMBEDDING_DIM
from koel.nlp.rag import BedrockTitanEmbedder, build_embedder


def _settings(**over):
    base = dict(
        nlp_embedder="bedrock",
        aws_region="ap-south-1",
        bedrock_embed_model_id="amazon.titan-embed-text-v2:0",
    )
    base.update(over)
    return SimpleNamespace(**base)


class _FakeBody:
    """Stands in for the streaming ``StreamingBody`` on a real invoke_model response."""

    def __init__(self, payload: dict) -> None:
        self._raw = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._raw


def _fake_invoke_model(payload: dict):
    def _invoke(**kwargs):  # noqa: ANN003 — mirrors boto3's invoke_model(**kwargs) signature
        return {"body": _FakeBody(payload)}

    return _invoke


async def test_bedrock_embedder_parses_api_response(monkeypatch):
    e = BedrockTitanEmbedder(region="ap-south-1")
    vec = [0.1] * EMBEDDING_DIM
    monkeypatch.setattr(
        e._client, "invoke_model", _fake_invoke_model({"embedding": vec, "inputTextTokenCount": 3})
    )
    out = await e.embed("namaste", input_type="query")
    assert len(out) == EMBEDDING_DIM
    assert out == vec


async def test_bedrock_embedder_falls_back_on_bad_shape(monkeypatch):
    e = BedrockTitanEmbedder(region="ap-south-1")
    monkeypatch.setattr(
        e._client, "invoke_model", _fake_invoke_model({"embedding": [0.1, 0.2]})  # wrong dim
    )
    out = await e.embed("x", input_type="query")
    assert len(out) == EMBEDDING_DIM  # degraded to the hash embedder, never wrong-dim


async def test_bedrock_embedder_falls_back_on_boto3_exception(monkeypatch):
    e = BedrockTitanEmbedder(region="ap-south-1")

    def _raise(**kwargs):  # noqa: ANN003 — mirrors boto3's invoke_model(**kwargs) signature
        raise RuntimeError("simulated boto3/AWS failure")

    monkeypatch.setattr(e._client, "invoke_model", _raise)
    out = await e.embed("x", input_type="query")
    assert len(out) == EMBEDDING_DIM  # degraded to the hash embedder, never raised


def test_build_embedder_selects_bedrock():
    embedder = build_embedder(_settings(nlp_embedder="bedrock"))
    assert isinstance(embedder, BedrockTitanEmbedder)

    # Case-insensitive, same as the other providers.
    embedder_upper = build_embedder(_settings(nlp_embedder="BEDROCK"))
    assert isinstance(embedder_upper, BedrockTitanEmbedder)
