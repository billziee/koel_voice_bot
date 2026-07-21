"""Auto node-builder: flow generation from RAG chunks (koel.nlp.flow_gen)."""

from __future__ import annotations

from types import SimpleNamespace

import koel.nlp.flow_gen as flow_gen
from koel.nlp.flow_gen import _clean_flow, generate_flow

# A valid model draft.
_GOOD = {
    "start": "greeting",
    "nodes": {
        "greeting": {"prompt": "Greet + ask for a minute.",
                     "branches": {"interested": "pitch", "busy": "close"}},
        "pitch": {"prompt": "Explain the offer.",
                  "branches": {"objection": "handle", "high_interest": "escalate"}},
        "handle": {"prompt": "Address the concern.", "branches": {"*": "pitch"}},
        "escalate": {"prompt": "Hand to a specialist.", "branches": {}},
        "close": {"prompt": "Thank and close.", "branches": {}},
    },
}


def test_clean_flow_accepts_a_valid_draft():
    out = _clean_flow(_GOOD)
    assert out is not None
    assert out["start"] == "greeting"
    assert set(out["nodes"]) == {"greeting", "pitch", "handle", "escalate", "close"}


def test_clean_flow_drops_bad_branches_and_dangling_targets():
    draft = {
        "start": "a",
        "nodes": {
            "a": {"prompt": "hi", "branches": {"interested": "b", "bogus_intent": "b", "busy": "ghost"}},
            "b": {"prompt": "there", "branches": {}},
        },
    }
    out = _clean_flow(draft)
    assert out is not None
    # bogus_intent (not an Intent) and busy→ghost (no such node) are dropped.
    assert out["nodes"]["a"]["branches"] == {"interested": "b"}


def test_clean_flow_repairs_missing_start():
    draft = {
        "start": "nonexistent",
        "nodes": {"x": {"prompt": "p", "branches": {}}, "y": {"prompt": "q", "branches": {}}},
    }
    out = _clean_flow(draft)
    assert out is not None and out["start"] in out["nodes"]


def test_clean_flow_rejects_unusable():
    assert _clean_flow(None) is None
    assert _clean_flow({"nodes": {}}) is None
    assert _clean_flow({"nodes": {"only": {"prompt": "p", "branches": {}}}}) is None  # < 2 nodes


async def test_generate_flow_none_without_llm_key():
    settings = SimpleNamespace(groq_api_key=None, groq_base_url="x", groq_model="m", groq_timeout_s=8.0)
    assert await generate_flow(chunks=[], use_case="collections", language="en", settings=settings) is None


async def test_generate_flow_parses_llm_json(monkeypatch):
    import json

    class _Resp:
        def raise_for_status(self):  # noqa: ANN201
            return None

        def json(self):  # noqa: ANN201
            return {"choices": [{"message": {"content": json.dumps(_GOOD)}}]}

    class _Client:
        async def post(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
            return _Resp()

        async def aclose(self):  # noqa: ANN201
            return None

    monkeypatch.setattr(flow_gen, "make_async_client", lambda **kw: _Client())
    settings = SimpleNamespace(
        groq_api_key="k", groq_base_url="https://api.groq.com/openai/v1",
        groq_model="llama", groq_timeout_s=8.0,
    )
    out = await generate_flow(
        chunks=[{"title": "Card", "content": "No annual fee."}],
        use_case="credit_card", language="en", settings=settings,
    )
    assert out is not None and out["start"] == "greeting" and len(out["nodes"]) == 5


async def test_generate_flow_none_on_llm_error(monkeypatch):
    class _Client:
        async def post(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
            raise RuntimeError("boom")

        async def aclose(self):  # noqa: ANN201
            return None

    monkeypatch.setattr(flow_gen, "make_async_client", lambda **kw: _Client())
    settings = SimpleNamespace(
        groq_api_key="k", groq_base_url="x", groq_model="m", groq_timeout_s=8.0
    )
    assert await generate_flow(chunks=[], use_case="x", language="en", settings=settings) is None
