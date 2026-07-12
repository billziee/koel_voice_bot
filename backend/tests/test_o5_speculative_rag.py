"""O-5 speculative RAG end-to-end seams.

  * NLP_svc ``POST /v1/retrieve`` returns chunks (or [] when RAG is off).
  * NLP_svc ``/respond`` reuses pre-fetched ``ctx.retrieved`` and SKIPS its own
    retrieval; empty ⇒ it retrieves as before.
  * The orchestrator threads ``prefetched_chunks`` into ``TurnContext.retrieved``;
    ``None`` leaves it empty (today's behaviour).
  * The WS handler fires a background pre-fetch on the smart-turn hold and, at
    turn time, reuses the chunks when the final hasn't diverged / drops them when
    it has. The no-smart-turn path never speculates.
"""

from __future__ import annotations

import asyncio
import contextlib

from koel.common.schemas import Language, Tone
from koel.nlp.schemas import Intent, NlpResponse, RetrievedChunk, TurnContext
from koel.stt.schemas import TranscriptResult
from koel.voicebot.config import VoiceBotSettings
from koel.voicebot.pipeline.bargein import BargeInClassifier
from koel.voicebot.pipeline.filler import FillerCache
from koel.voicebot.pipeline.orchestrator import Orchestrator
from koel.voicebot.session.models import SessionState, Turn
from koel.voicebot.ws.handler import CallSession
from tests.conftest import FakeClients, FakeStore

_CHUNK = RetrievedChunk(title="Regalia", content="airport lounge access", score=0.9)


def _ctx(**over) -> TurnContext:
    base = dict(
        session_id="s", bot_id="b", use_case="credit_card", language=Language.hinglish,
        customer_text="annual fee kitni hai", rag_enabled=True, rag_index="cc_hdfc",
    )
    base.update(over)
    return TurnContext(**base)


# --- NLP_svc /v1/retrieve --------------------------------------------------


async def test_retrieve_route_returns_chunks(monkeypatch):
    from koel.nlp import main as nlp_main

    async def fake_retrieve(query, **kwargs):  # noqa: ANN001, ANN003
        assert query == "annual fee kitni hai"
        return [_CHUNK]

    monkeypatch.setattr(nlp_main, "retrieve", fake_retrieve)
    out = await nlp_main.retrieve_route(_ctx(), None)
    assert [c.title for c in out] == ["Regalia"]


async def test_retrieve_route_empty_when_rag_disabled():
    from koel.nlp import main as nlp_main

    # rag_enabled False → no DB touched, degrade-safe [].
    out = await nlp_main.retrieve_route(_ctx(rag_enabled=False), None)
    assert out == []


async def test_retrieve_route_empty_when_no_index():
    from koel.nlp import main as nlp_main

    out = await nlp_main.retrieve_route(_ctx(rag_index=None), None)
    assert out == []


# --- NLP_svc /respond honours pre-fetched chunks ---------------------------


class _CaptureProvider:
    def __init__(self) -> None:
        self.retrieved: list[RetrievedChunk] | None = None

    async def generate(self, ctx, retrieved):  # noqa: ANN001
        self.retrieved = retrieved
        return NlpResponse(
            session_id=ctx.session_id, response_text="ok", intent=Intent.interested,
            tone=Tone.cheerful,
        )


def _spy_retrieve(monkeypatch):
    """Patch _retrieve_for with a call-counting spy; return the counter dict."""
    from koel.nlp import main as nlp_main

    calls = {"n": 0}

    async def spy(ctx):  # noqa: ANN001
        calls["n"] += 1
        return []

    monkeypatch.setattr(nlp_main, "_retrieve_for", spy)
    return calls


async def test_respond_uses_prefetched_and_skips_retrieval(monkeypatch):
    from koel.nlp import main as nlp_main

    calls = _spy_retrieve(monkeypatch)
    provider = _CaptureProvider()
    monkeypatch.setattr(nlp_main, "_provider", provider)

    resp = await nlp_main.respond(_ctx(retrieved=[_CHUNK]), None)

    assert calls["n"] == 0  # pre-fetched ⇒ NO second retrieval
    assert provider.retrieved is not None
    assert [c.title for c in provider.retrieved] == ["Regalia"]  # flowed into generation
    assert resp.response_text == "ok"


async def test_respond_retrieves_when_no_prefetch(monkeypatch):
    from koel.nlp import main as nlp_main

    calls = _spy_retrieve(monkeypatch)
    monkeypatch.setattr(nlp_main, "_provider", _CaptureProvider())

    await nlp_main.respond(_ctx(), None)  # no retrieved on the ctx

    assert calls["n"] == 1  # falls back to retrieving now, exactly as before


# --- Orchestrator threads prefetched_chunks into ctx.retrieved -------------


class _CapturingClients(FakeClients):
    """Captures each turn's TurnContext and returns a canned reply."""

    def __init__(self) -> None:
        super().__init__()
        self.ctxs: list[TurnContext] = []

    async def respond(self, ctx):  # noqa: ANN001
        self.ctxs.append(ctx)
        return NlpResponse(
            session_id=ctx.session_id, response_text="ok", intent=Intent.interested,
            tone=Tone.neutral,
        )


def _orch(clients) -> Orchestrator:  # noqa: ANN001
    # stream_nlp off (default) → batch _respond_with_filler → clients.respond.
    return Orchestrator(settings=VoiceBotSettings(), clients=clients, filler_cache=FillerCache())


def _turn_state() -> SessionState:
    return SessionState(
        session_id="s", bot_id="b", customer_id="c1", use_case="credit_card",
        language=Language.hinglish, rag_enabled=True, rag_index="cc_hdfc",
    )


def _stt(text: str) -> TranscriptResult:
    return TranscriptResult(session_id="s", text=text, is_final=True, language_id=Language.hinglish)


async def _noop_send(_message) -> None:  # noqa: ANN001
    return None


async def test_orchestrator_threads_prefetched_into_ctx():
    clients = _CapturingClients()
    await _orch(clients).handle_final_transcript(
        _turn_state(), _noop_send, _stt("annual fee kitni hai"), prefetched_chunks=[_CHUNK]
    )
    assert [c.title for c in clients.ctxs[-1].retrieved] == ["Regalia"]


async def test_orchestrator_no_prefetch_leaves_retrieved_empty():
    clients = _CapturingClients()
    await _orch(clients).handle_final_transcript(
        _turn_state(), _noop_send, _stt("annual fee kitni hai")
    )
    assert clients.ctxs[-1].retrieved == []  # byte-for-byte with today


# --- WS handler: fire on hold, reuse / drop at turn time -------------------


class _RetrieveClients(FakeClients):
    """FakeClients with the O-5 ``retrieve`` method returning canned chunks."""

    def __init__(self, chunks: list[RetrievedChunk]) -> None:
        super().__init__()
        self._chunks = chunks
        self.retrieve_calls = 0

    async def retrieve(self, ctx):  # noqa: ANN001
        self.retrieve_calls += 1
        return list(self._chunks)


def _session(clients, **over) -> CallSession:  # noqa: ANN001
    settings = VoiceBotSettings(heartbeat_interval_seconds=999, **over)
    orch = Orchestrator(settings=settings, clients=clients, filler_cache=FillerCache())
    state = SessionState(
        session_id="ws", bot_id="b", customer_id="c1", use_case="credit_card",
        language=Language.hinglish, rag_enabled=True, rag_index="cc_hdfc",
    )
    # A held transcript is built from an existing turn context; seed the greeting.
    state.add_turn(Turn(index=0, speaker="bot", text="Namaste"))
    return CallSession(
        websocket=None, state=state, settings=settings, store=FakeStore(),
        orchestrator=orch, bargein=BargeInClassifier(backchannel_max_speech_ms=300),
        clients=clients, resumed=False,
    )


async def test_speculative_fire_then_reuse_when_not_diverged():
    clients = _RetrieveClients([_CHUNK])
    call = _session(clients)

    call._start_speculative_retrieval("annual fee kitni hai")  # noqa: SLF001
    assert call._spec_retrieval is not None  # noqa: SLF001

    out = await call._resolve_prefetched_chunks("annual fee kitni hai batao")  # noqa: SLF001
    assert clients.retrieve_calls == 1
    assert out is not None and [c.title for c in out] == ["Regalia"]
    assert call._spec_retrieval is None  # noqa: SLF001 — consumed


async def test_speculative_dropped_when_diverged():
    clients = _RetrieveClients([_CHUNK])
    call = _session(clients)

    call._start_speculative_retrieval("annual fee kitni hai")  # noqa: SLF001
    _, task = call._spec_retrieval  # noqa: SLF001

    # A wholly different final question ⇒ the speculative chunks are discarded and
    # NLP_svc will retrieve for the real query instead.
    out = await call._resolve_prefetched_chunks("mujhe personal loan chahiye")  # noqa: SLF001
    assert out is None
    assert call._spec_retrieval is None  # noqa: SLF001
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.cancelled()


async def test_speculative_skipped_when_rag_disabled():
    clients = _RetrieveClients([_CHUNK])
    call = _session(clients)
    call._state.rag_enabled = False  # noqa: SLF001

    call._start_speculative_retrieval("annual fee kitni hai")  # noqa: SLF001
    assert call._spec_retrieval is None  # noqa: SLF001
    assert clients.retrieve_calls == 0


async def test_speculative_skipped_when_kill_switch_off():
    clients = _RetrieveClients([_CHUNK])
    call = _session(clients, speculative_rag_enabled=False)

    call._start_speculative_retrieval("annual fee kitni hai")  # noqa: SLF001
    assert call._spec_retrieval is None  # noqa: SLF001
    assert clients.retrieve_calls == 0


async def test_speculative_skipped_when_client_lacks_retrieve():
    # A plain FakeClients has no ``retrieve`` method → speculation is skipped.
    call = _session(FakeClients())
    call._start_speculative_retrieval("annual fee kitni hai")  # noqa: SLF001
    assert call._spec_retrieval is None  # noqa: SLF001


async def test_new_hold_cancels_stale_speculative_task():
    clients = _RetrieveClients([_CHUNK])
    call = _session(clients)

    call._start_speculative_retrieval("pehla sawaal")  # noqa: SLF001
    _, first = call._spec_retrieval  # noqa: SLF001
    call._start_speculative_retrieval("doosra sawaal")  # noqa: SLF001 — replaces the first
    with contextlib.suppress(asyncio.CancelledError):
        await first
    assert first.cancelled()
    assert call._spec_retrieval is not None  # noqa: SLF001 — the newest is in flight
    # Drain the live one so the loop tears down cleanly.
    await call._resolve_prefetched_chunks("doosra sawaal")  # noqa: SLF001


async def test_no_speculation_without_a_hold():
    # No smart-turn gate ⇒ no hold ⇒ _resolve short-circuits (None) and nothing is
    # ever pre-fetched: the batch / no-smart-turn path is unchanged.
    clients = _RetrieveClients([_CHUNK])
    call = _session(clients)
    out = await call._resolve_prefetched_chunks("annual fee kitni hai")  # noqa: SLF001
    assert out is None
    assert clients.retrieve_calls == 0
    assert call._spec_retrieval is None  # noqa: SLF001
