"""Per-bot STT mode switch (roadmap O-6).

Covers the two seams that make ``stt_mode`` a real per-bot switch:

1. ``build_session_state`` maps ``BotConfig.config["stt_mode"]`` onto
   ``SessionState.stt_mode`` — "ws" opts in, absent/invalid falls back to "chunk".
2. ``CallSession._handle_audio`` routes a ws-mode final turn through the client's
   streaming STT (``stream_transcribe``) and a chunk-mode turn through batch
   ``transcribe`` — either way the turn reaches ``handle_final_transcript`` with
   the transcript that path produced.
"""

from __future__ import annotations

import json

from koel.common.schemas import Language, Tone
from koel.db.models import BotConfig, UseCase
from koel.nlp.schemas import Intent, NlpResponse
from koel.stt.schemas import TranscribeRequest, TranscriptResult
from koel.voicebot.api.sessions import build_session_state
from tests.conftest import FakeClients
from tests.test_ws_session import FakeWebSocket, _session, _state


def _bot(config: dict) -> BotConfig:
    """A transient BotConfig with every field build_session_state reads set
    explicitly (SQLAlchemy column defaults don't apply until INSERT)."""
    return BotConfig(
        id="bot_x", name="B", use_case=UseCase.credit_card, language="hinglish",
        voice_gender="female", speaking_rate=1.0, greeting_template="",
        rag_enabled=True, rag_index="cc_hdfc", default_tone="neutral",
        ambient_profile="callfloor", active=True, config=config,
    )


# --- 1. build_session_state config → stt_mode --------------------------------


def test_ws_mode_seeded_from_config():
    state = build_session_state(_bot({"stt_mode": "ws"}), customer_id=None)
    assert state.stt_mode == "ws"


def test_absent_stt_mode_defaults_to_chunk():
    state = build_session_state(_bot({}), customer_id=None)
    assert state.stt_mode == "chunk"


def test_invalid_stt_mode_falls_back_to_chunk():
    state = build_session_state(_bot({"stt_mode": "streaming-please"}), customer_id=None)
    assert state.stt_mode == "chunk"


# --- 2. _handle_audio transport selection ------------------------------------


class _ModeClients(FakeClients):
    """Distinct transcripts per transport so a turn's source is observable."""

    def __init__(self) -> None:
        super().__init__()
        self.batch_calls = 0
        self.stream_calls = 0

    async def transcribe(self, request):  # noqa: ANN001
        self.batch_calls += 1
        return self._result(request, "BATCH")

    async def stream_transcribe(self, request):  # noqa: ANN001
        self.stream_calls += 1
        yield self._result(request, "STREAMED")

    async def respond(self, ctx):  # noqa: ANN001
        return NlpResponse(
            session_id=ctx.session_id, response_text="theek hai", intent=Intent.interested,
            tone=Tone.cheerful, confidence=0.9,
        )

    @staticmethod
    def _result(request: TranscribeRequest, text: str) -> TranscriptResult:
        return TranscriptResult(
            session_id=request.session_id, text=text, is_final=True,
            language_id=Language.hinglish, language_confidence=0.95,
            speech_duration_ms=600, word_confidence=0.9,
        )


async def _drive_turn(stt_mode: str) -> tuple[_ModeClients, dict]:
    audio = json.dumps({"type": "audio_chunk", "audio_b64": "x", "is_final": True})
    ws = FakeWebSocket([audio])
    clients = _ModeClients()
    state = _state(f"ws_{stt_mode}")
    state.stt_mode = stt_mode
    call = _session(ws, state, clients)

    captured: dict = {}

    async def _spy(state, send, stt, prefetched_chunks=None):  # noqa: ANN001
        captured["text"] = stt.text
        captured["is_final"] = stt.is_final

    call._orch.handle_final_transcript = _spy  # type: ignore[method-assign]  # noqa: SLF001
    await call.run()
    return clients, captured


async def test_ws_mode_turn_uses_stream_transcribe(test_db):
    clients, captured = await _drive_turn("ws")

    # The streamed transcript reached the orchestrator; batch was never used.
    assert captured["text"] == "STREAMED"
    assert captured["is_final"] is True
    assert clients.stream_calls == 1
    assert clients.batch_calls == 0


async def test_chunk_mode_turn_uses_batch_transcribe(test_db):
    clients, captured = await _drive_turn("chunk")

    # The batch transcript reached the orchestrator; the stream was never touched.
    assert captured["text"] == "BATCH"
    assert captured["is_final"] is True
    assert clients.batch_calls == 1
    assert clients.stream_calls == 0
