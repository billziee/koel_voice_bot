"""O-2 orchestrator activation: the streaming bot turn (stream_nlp).

Verifies the turn is spoken sentence-by-sentence as NLP streams, the transcript
lands once with the full reply, control flow still applies without re-speaking,
and a stream failure degrades to the batch path (never drops the turn)."""

from __future__ import annotations

import asyncio

import pytest

from koel.common.schemas import Language, Tone
from koel.common.text import split_sentences
from koel.nlp.schemas import Intent, NlpResponse, NlpStreamFrame
from koel.stt.schemas import TranscriptResult
from koel.tts.providers import StubOrpheusProvider
from koel.voicebot.config import VoiceBotSettings
from koel.voicebot.pipeline.filler import FillerCache
from koel.voicebot.pipeline.orchestrator import Orchestrator
from koel.voicebot.session.models import CallStatus, SessionState
from koel.voicebot.ws.protocol import ServerMessageType


class _StreamClients:
    """Yields the reply as sentence frames + a final frame; records batch fallback."""

    def __init__(
        self,
        nlp: NlpResponse,
        *,
        stream_raises: bool = False,
        raise_after: int | None = None,
        raise_exc: type[BaseException] = RuntimeError,
        empty: bool = False,
    ) -> None:
        self._nlp = nlp
        self._stream_raises = stream_raises
        self._raise_after = raise_after  # break after N sentence frames, before final
        self._raise_exc = raise_exc
        self._empty = empty  # normal completion, zero sentences, empty-text final
        self.batch_respond_calls = 0

    async def respond_stream(self, ctx):  # noqa: ANN001
        if self._stream_raises:
            raise self._raise_exc("nlp stream transport error")
            yield  # pragma: no cover — makes this an async generator
        if self._empty:
            yield NlpStreamFrame(type="final", response=self._nlp.model_copy(update={"response_text": ""}))
            return
        n = 0
        for sentence in split_sentences(self._nlp.response_text):
            yield NlpStreamFrame(type="sentence", text=sentence)
            n += 1
            if self._raise_after is not None and n >= self._raise_after:
                raise self._raise_exc("stream broke before the final frame")
        yield NlpStreamFrame(type="final", response=self._nlp)

    async def respond(self, ctx):  # noqa: ANN001 — batch fallback
        self.batch_respond_calls += 1
        return self._nlp

    async def stream_synthesis(self, request):  # noqa: ANN001
        async for chunk in StubOrpheusProvider()._stream_by_sentence(request):  # noqa: SLF001
            yield chunk

    async def flag_customer(self, customer_id, flag, reason=None):  # noqa: ANN001
        return None


def _settings() -> VoiceBotSettings:
    return VoiceBotSettings(stream_nlp=True, stream_tts=True)


def _state() -> SessionState:
    return SessionState(
        session_id="s", bot_id="b", customer_id="cust_1", use_case="credit_card",
        language=Language.hinglish, crm_context={"name": "Rahul"},
    )


def _stt() -> TranscriptResult:
    return TranscriptResult(
        session_id="s", text="haan mujhe interest hai, detail batao",
        is_final=True, language_id=Language.hinglish, language_confidence=0.95,
    )


async def _noop() -> None:
    return None


async def _drive(clients, state: SessionState) -> list:
    orch = Orchestrator(settings=_settings(), clients=clients, filler_cache=FillerCache())
    sent: list = []
    await orch.handle_final_transcript(state, lambda m: sent.append(m) or _noop(), _stt())
    return sent


def _reply(text: str, **over) -> NlpResponse:
    base = dict(session_id="s", response_text=text, intent=Intent.interested, tone=Tone.cheerful)
    base.update(over)
    return NlpResponse(**base)


def _bot_transcripts(sent: list) -> list:
    return [
        m for m in sent
        if m.type == ServerMessageType.transcript and m.data.get("speaker") == "bot"
    ]


async def test_streaming_turn_speaks_sentences_and_lands_listening():
    text = "Great! Yeh card aapke liye perfect hai. Benefits bataati hoon. Interested hain?"
    clients = _StreamClients(_reply(text))
    state = _state()
    sent = await _drive(clients, state)

    audio = [m for m in sent if m.type == ServerMessageType.audio_chunk]
    transcripts = _bot_transcripts(sent)
    assert audio, "no audio streamed"
    # Exactly one bot transcript for the turn, carrying the full reply.
    assert len(transcripts) == 1
    assert transcripts[-1].data["text"] == text
    # Streaming was used, not the batch fallback.
    assert clients.batch_respond_calls == 0
    assert state.status == CallStatus.listening
    # audio chunk seq is monotonic across the streamed sentences.
    seqs = [m.data["seq"] for m in audio]
    assert seqs == sorted(seqs)


async def test_streaming_turn_opt_out_ends_without_re_speaking():
    text = "Bilkul, main aapka number remove kar deti hoon. Dhanyavaad."
    clients = _StreamClients(_reply(text, intent=Intent.opt_out))
    state = _state()
    sent = await _drive(clients, state)

    bot_transcripts = _bot_transcripts(sent)
    # Spoken exactly once (streamed), not again by the opt-out branch.
    assert [t.data["text"] for t in bot_transcripts].count(text) == 1
    assert state.status == CallStatus.ended
    assert state.outcome == "declined"


async def test_streaming_turn_degrades_to_batch_on_stream_error():
    text = "Koi baat nahi, main phir call karti hoon."
    clients = _StreamClients(_reply(text), stream_raises=True)
    state = _state()
    sent = await _drive(clients, state)

    # Stream failed before any audio → batch respond() spoke the turn instead.
    assert clients.batch_respond_calls == 1
    assert [m for m in sent if m.type == ServerMessageType.audio_chunk]
    bot_transcripts = _bot_transcripts(sent)
    assert bot_transcripts and bot_transcripts[-1].data["text"] == text


async def test_lost_final_after_partial_audio_recovers_intent_from_batch():
    # Review #1: the stream speaks a sentence then breaks BEFORE the final frame.
    # The structured fields (here opt_out) must be recovered via batch respond(),
    # not silently dropped — a lost opt-out would be a TRAI violation.
    reply = _reply("Bilkul, main aapka number remove kar deti hoon.", intent=Intent.opt_out)
    clients = _StreamClients(reply, raise_after=1)
    state = _state()
    await _drive(clients, state)
    assert clients.batch_respond_calls == 1  # reconciled the lost final frame
    assert state.status == CallStatus.ended and state.outcome == "declined"  # opt-out honoured


async def test_empty_stream_speaks_via_batch_not_silence():
    # Review #2: a stream that completes with no sentence frames must not leave a
    # silent bot_speaking→listening flip — it falls back to batch and speaks.
    text = "Aapka din shubh ho. Main baad mein call karti hoon."
    clients = _StreamClients(_reply(text), empty=True)
    state = _state()
    sent = await _drive(clients, state)
    assert clients.batch_respond_calls == 1
    assert [m for m in sent if m.type == ServerMessageType.audio_chunk]  # audio, not silence
    assert _bot_transcripts(sent)[-1].data["text"] == text


async def test_streamed_turn_persists_resolved_tone_not_default():
    # Review #3: the persisted transcript carries final.tone (empathetic here),
    # not the default streaming-prosody tone (credit_card default is cheerful).
    clients = _StreamClients(_reply("Main samajh sakti hoon.", tone=Tone.empathetic))
    state = _state()
    sent = await _drive(clients, state)
    assert _bot_transcripts(sent)[-1].data["tone"] == Tone.empathetic.value


async def test_cancellation_mid_stream_still_records_partial_turn():
    # Review #4: a CancelledError mid-stream (server shutdown) propagates, but the
    # partial bot turn is still recorded so the audit trail isn't missing it.
    reply = _reply("Pehli baat ho gayi. Doosri aa rahi.")
    clients = _StreamClients(reply, raise_after=1, raise_exc=asyncio.CancelledError)
    state = _state()
    with pytest.raises(asyncio.CancelledError):
        await _drive(clients, state)
    bot_turns = [t for t in state.transcript if t.speaker == "bot"]
    assert bot_turns and bot_turns[-1].text  # the partial turn was persisted
