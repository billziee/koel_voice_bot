"""StreamingTelephonyIngest + the /twilio/stream ingest-selection wiring (O-6 Wave 2).

Two layers, both hermetic (NO live key):

A. StreamingTelephonyIngest drives a FAKE streaming session: interims surface on
   ``on_partial``, finals on ``on_utterance``; a start failure reports False (never
   raises) and a mid-stream send failure degrades safely (feeds become no-ops, the
   call is never dropped).

B. ``main._build_telephony_ingest`` picks the STREAMING ingest for a Deepgram call
   (env default OR a bot Deepgram STT connection) and the BUFFERED
   ``TelephonyAudioIngest`` otherwise or when the Deepgram socket won't open — the
   route-level selection + degrade chain, with ``connect`` monkeypatched.
"""

from __future__ import annotations

import asyncio
import json

import websockets.asyncio.client as ws_client

from koel.common.model_connections import ResolvedConnection
from koel.common.schemas import Language
from koel.stt.schemas import TranscriptResult
from koel.voicebot.session.models import SessionState
from koel.voicebot.telephony.ingest import (
    StreamingTelephonyIngest,
    TelephonyAudioIngest,
)

# --- A. StreamingTelephonyIngest over a fake session --------------------------


class _FakeSession:
    """Fake StreamingSttSession: emits an interim per frame and a final on finish.
    ``fail_on`` ("start" | "send") injects a failure at that stage."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.on_partial = None
        self.on_final = None
        self._fail_on = fail_on
        self.started = False
        self.finished = False
        self.closed = False
        self.frames: list[bytes] = []

    async def start(self) -> None:
        if self._fail_on == "start":
            raise ConnectionRefusedError("deepgram unreachable")
        self.started = True

    async def send_frame(self, pcm16: bytes) -> None:
        if self._fail_on == "send":
            raise RuntimeError("socket send broke")
        self.frames.append(pcm16)
        if self.on_partial is not None:
            await self.on_partial(f"interim {len(self.frames)}")

    async def finish(self) -> None:
        self.finished = True
        if self.on_final is not None:
            await self.on_final(
                TranscriptResult(session_id="s", text="final utterance", is_final=True)
            )

    async def aclose(self) -> None:
        self.closed = True


def _factory_for(session: _FakeSession):  # noqa: ANN202
    def factory(on_partial, on_final):  # noqa: ANN001, ANN202
        session.on_partial = on_partial
        session.on_final = on_final
        return session

    return factory


class _Recorder:
    def __init__(self) -> None:
        self.items: list = []

    async def __call__(self, item) -> None:  # noqa: ANN001
        self.items.append(item)


class _FinalRecorder:
    """Like _Recorder, but for on_utterance's (TranscriptResult, pcm16) pair."""

    def __init__(self) -> None:
        self.items: list = []
        self.audio: list[bytes] = []

    async def __call__(self, result, pcm16) -> None:  # noqa: ANN001
        self.items.append(result)
        self.audio.append(pcm16)


async def test_partials_and_final_are_surfaced():
    session = _FakeSession()
    partials, finals = _Recorder(), _FinalRecorder()
    ingest = StreamingTelephonyIngest(
        session_factory=_factory_for(session), on_utterance=finals, on_partial=partials
    )

    assert await ingest.start() is True
    await ingest.feed(b"\x00\x01" * 10, 16000)
    await ingest.feed(b"\x00\x01" * 10, 16000)
    await ingest.flush()

    # Interims surfaced live on on_partial; the final surfaced on on_utterance.
    assert partials.items == ["interim 1", "interim 2"]
    assert len(finals.items) == 1
    assert finals.items[0].text == "final utterance"
    assert session.frames  # frames were pushed into the session
    assert session.finished is True
    # The rolling audio window (Deepgram gives text only) rode along with the
    # final — the smart-turn gate needs the waveform.
    assert finals.audio[0] == b"\x00\x01" * 20

    await ingest.aclose()
    assert session.closed is True


async def test_blank_partial_and_non_final_are_dropped():
    session = _FakeSession()
    partials, finals = _Recorder(), _FinalRecorder()
    ingest = StreamingTelephonyIngest(
        session_factory=_factory_for(session), on_utterance=finals, on_partial=partials
    )
    await ingest.start()

    # A blank interim must not be surfaced; a non-final result is not a turn.
    await session.on_partial("   ")
    await session.on_final(TranscriptResult(session_id="s", text="partial", is_final=False))
    await session.on_final(TranscriptResult(session_id="s", text="  ", is_final=True))

    assert partials.items == []
    assert finals.items == []


async def test_no_partial_sink_is_fine():
    session = _FakeSession()
    finals = _FinalRecorder()
    ingest = StreamingTelephonyIngest(
        session_factory=_factory_for(session), on_utterance=finals  # on_partial=None
    )
    await ingest.start()
    await ingest.feed(b"\x00\x01" * 10, 16000)  # session emits a partial → no sink, no crash
    await ingest.flush()

    assert len(finals.items) == 1


async def test_start_failure_reports_false_and_degrades():
    session = _FakeSession(fail_on="start")
    partials, finals = _Recorder(), _FinalRecorder()
    ingest = StreamingTelephonyIngest(
        session_factory=_factory_for(session), on_utterance=finals, on_partial=partials
    )

    assert await ingest.start() is False  # never raises
    await ingest.feed(b"\x00\x01" * 10, 16000)  # no-op — not started
    assert session.frames == []
    assert partials.items == []
    await ingest.aclose()  # still tears down cleanly


async def test_send_failure_degrades_safely():
    session = _FakeSession(fail_on="send")
    partials, finals = _Recorder(), _FinalRecorder()
    ingest = StreamingTelephonyIngest(
        session_factory=_factory_for(session), on_utterance=finals, on_partial=partials
    )
    assert await ingest.start() is True

    # A session send error is swallowed (never raised into the live call)…
    await ingest.feed(b"\x00\x01" * 10, 16000)
    # …and once degraded, further feeds are silent no-ops.
    await ingest.feed(b"\x00\x01" * 10, 16000)

    assert partials.items == []
    assert finals.items == []
    await ingest.aclose()  # never raises


async def test_audio_window_resets_between_utterances():
    session = _FakeSession()
    finals = _FinalRecorder()
    ingest = StreamingTelephonyIngest(
        session_factory=_factory_for(session), on_utterance=finals
    )
    await ingest.start()

    await ingest.feed(b"\x01\x02" * 5, 16000)
    await ingest.flush()  # first utterance: finish() fires one on_final
    assert finals.audio[0] == b"\x01\x02" * 5

    # A second utterance's window must not carry over the first's audio.
    await ingest.feed(b"\x03\x04" * 5, 16000)
    await ingest.flush()
    assert finals.audio[1] == b"\x03\x04" * 5


# --- B. main._build_telephony_ingest selection + degrade ----------------------


class _FakeConnect:
    def __init__(self, ws: _FakeWS) -> None:
        self._ws = ws

    def __await__(self):  # noqa: ANN204
        async def _c() -> _FakeWS:
            return self._ws

        return _c().__await__()


class _FakeWS:
    """Minimal Deepgram socket for the route-selection tests: yields a single
    Metadata frame so the session's reader ends immediately, then closes."""

    def __init__(self) -> None:
        self._sent_metadata = False
        self.closed = False

    async def send(self, data) -> None:  # noqa: ANN001
        return None

    async def recv(self) -> str:
        if not self._sent_metadata:
            self._sent_metadata = True
            return json.dumps({"type": "Metadata"})
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def close(self) -> None:
        self.closed = True


def _state(**kw) -> SessionState:  # noqa: ANN003
    base = dict(session_id="s", bot_id="b", use_case="credit_card", language=Language.hinglish)
    return SessionState(**{**base, **kw})  # type: ignore[arg-type]


async def _noop_transcribe(pcm16: bytes, sample_rate: int) -> TranscriptResult | None:  # noqa: ARG001
    return None


async def _noop_utterance(stt: TranscriptResult, pcm16: bytes) -> None:  # noqa: ARG001
    return None


async def _noop_partial(text: str) -> None:  # noqa: ARG001
    return None


def _patch_streaming_socket(monkeypatch) -> _FakeWS:  # noqa: ANN001
    fake = _FakeWS()
    monkeypatch.setattr(ws_client, "connect", lambda *a, **k: _FakeConnect(fake))
    return fake


async def test_build_ingest_selects_streaming_for_env_default_deepgram(monkeypatch):
    from koel.voicebot import main

    fake = _patch_streaming_socket(monkeypatch)
    monkeypatch.setattr(main.stt_settings, "stream_stt_provider", "deepgram")
    monkeypatch.setattr(main.stt_settings, "deepgram_api_key", "dg-key")

    ingest = await main._build_telephony_ingest(
        state=_state(stt_connection_id=None),
        transcribe=_noop_transcribe,
        on_utterance=_noop_utterance,
        on_partial=_noop_partial,
    )
    assert isinstance(ingest, StreamingTelephonyIngest)
    await ingest.aclose()
    assert fake.closed is True


async def test_build_ingest_selects_streaming_for_deepgram_byok_connection(monkeypatch):
    from koel.voicebot import main

    _patch_streaming_socket(monkeypatch)
    # Env default OFF → prove the bot's Deepgram STT connection is what selects it.
    monkeypatch.setattr(main.stt_settings, "stream_stt_provider", "sarvam")
    monkeypatch.setattr(main.stt_settings, "deepgram_api_key", None)

    async def fake_resolve(conn_id, org_id=None):  # noqa: ANN001, ANN202, ARG001
        return ResolvedConnection(
            id=conn_id, kind="stt", provider="deepgram",
            base_url="https://api.deepgram.com", model="nova-3", api_key="byok-key",
        )

    monkeypatch.setattr(main, "resolve_connection", fake_resolve)

    ingest = await main._build_telephony_ingest(
        state=_state(stt_connection_id="conn_dg", org_id="org1"),
        transcribe=_noop_transcribe,
        on_utterance=_noop_utterance,
        on_partial=_noop_partial,
    )
    assert isinstance(ingest, StreamingTelephonyIngest)
    await ingest.aclose()


async def test_build_ingest_uses_buffered_when_not_deepgram(monkeypatch):
    from koel.voicebot import main

    monkeypatch.setattr(main.stt_settings, "stream_stt_provider", "sarvam")
    monkeypatch.setattr(main.stt_settings, "deepgram_api_key", None)

    ingest = await main._build_telephony_ingest(
        state=_state(stt_connection_id=None),
        transcribe=_noop_transcribe,
        on_utterance=_noop_utterance,
        on_partial=_noop_partial,
    )
    assert isinstance(ingest, TelephonyAudioIngest)


async def test_build_ingest_degrades_to_buffered_when_socket_fails(monkeypatch):
    from koel.voicebot import main

    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise ConnectionRefusedError("deepgram unreachable")

    monkeypatch.setattr(ws_client, "connect", boom)
    monkeypatch.setattr(main.stt_settings, "stream_stt_provider", "deepgram")
    monkeypatch.setattr(main.stt_settings, "deepgram_api_key", "dg-key")

    ingest = await main._build_telephony_ingest(
        state=_state(stt_connection_id=None),
        transcribe=_noop_transcribe,
        on_utterance=_noop_utterance,
        on_partial=_noop_partial,
    )
    # Socket wouldn't open → degrade to the buffered ingest (call never dropped).
    assert isinstance(ingest, TelephonyAudioIngest)
