"""Semantic end-of-turn gate (smart-turn v3.2): hold-and-merge + graceful degrade.

The handler-level tests drive _handle_audio directly with a fake analyzer so the
merge state machine is deterministic (no receive-loop races); the analyzer itself
gets a real-inference smoke test that only runs when the model file is present.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest

from koel.voicebot.pipeline.endpointing import SmartTurnAnalyzer, merge_transcripts
from koel.voicebot.ws.protocol import ClientMessage, ClientMessageType, ServerMessageType
from tests.test_ws_session import FakeWebSocket, ScriptedClients, _nlp, _state

_MODEL = Path(__file__).parent.parent / "models" / "smart-turn-v3.2-cpu.onnx"
_PCM_B64 = base64.b64encode(b"\x00\x00" * 1600).decode()  # 100ms silence @16k


def test_merge_transcripts_joins_and_strips():
    assert merge_transcripts("main soch raha tha ki ", " emi kitni hogi") == (
        "main soch raha tha ki emi kitni hogi"
    )
    assert merge_transcripts("", "hello") == "hello"


async def test_analyzer_degrades_to_none_when_model_missing(tmp_path):
    analyzer = SmartTurnAnalyzer(model_path=str(tmp_path / "nope.onnx"))
    assert await analyzer.is_complete(_PCM_B64) is None
    # Stays disabled (no retry storm) and never raises.
    assert await analyzer.is_complete(_PCM_B64) is None


@pytest.mark.skipif(not _MODEL.is_file(), reason="model not fetched")
async def test_analyzer_real_inference_returns_verdict():
    analyzer = SmartTurnAnalyzer(model_path=str(_MODEL))
    verdict = await analyzer.is_complete(_PCM_B64)
    assert isinstance(verdict, bool)


class FakeSmartTurn:
    def __init__(self, verdict: bool | None) -> None:
        self.verdict = verdict
        self.calls = 0

    async def is_complete(self, pcm16_b64: str) -> bool | None:
        self.calls += 1
        return self.verdict


def _call(ws, clients, *, verdict, hold_ms=60):
    from tests.test_ws_session import _session

    call = _session(ws, _state(), clients, smart_turn_hold_ms=hold_ms)
    call._smart_turn = FakeSmartTurn(verdict)  # noqa: SLF001
    return call


def _audio_msg() -> ClientMessage:
    return ClientMessage(
        type=ClientMessageType.audio_chunk, audio_b64="x", is_final=True, pcm16_b64=_PCM_B64
    )


async def test_incomplete_turn_is_held_then_merged_with_followup(test_db):
    ws = FakeWebSocket([])
    clients = ScriptedClients(nlp=_nlp(), stt_text="main soch raha tha ki")
    call = _call(ws, clients, verdict=False)

    await call._handle_audio(_audio_msg())  # noqa: SLF001
    # Held: no customer transcript emitted yet, no bot reply.
    assert ws.frames_of(ServerMessageType.transcript) == []

    clients._stt_text = "emi kitni hogi"  # noqa: SLF001
    await call._handle_audio(_audio_msg())  # noqa: SLF001

    transcripts = ws.frames_of(ServerMessageType.transcript)
    customer = [t for t in transcripts if t["speaker"] == "customer"]
    assert len(customer) == 1  # ONE merged turn, not two
    assert customer[0]["text"] == "main soch raha tha ki emi kitni hogi"


async def test_held_turn_flushes_alone_after_window(test_db):
    ws = FakeWebSocket([])
    clients = ScriptedClients(nlp=_nlp(), stt_text="main soch raha tha ki")
    call = _call(ws, clients, verdict=False, hold_ms=50)

    await call._handle_audio(_audio_msg())  # noqa: SLF001
    assert ws.frames_of(ServerMessageType.transcript) == []

    await asyncio.sleep(0.15)  # let the flush timer fire
    customer = [
        t for t in ws.frames_of(ServerMessageType.transcript) if t["speaker"] == "customer"
    ]
    assert len(customer) == 1
    assert customer[0]["text"] == "main soch raha tha ki"


async def test_complete_turn_passes_straight_through(test_db):
    ws = FakeWebSocket([])
    clients = ScriptedClients(nlp=_nlp(), stt_text="mujhe card nahi chahiye")
    call = _call(ws, clients, verdict=True)

    await call._handle_audio(_audio_msg())  # noqa: SLF001
    customer = [
        t for t in ws.frames_of(ServerMessageType.transcript) if t["speaker"] == "customer"
    ]
    assert len(customer) == 1  # answered immediately, no hold


async def test_gate_unavailable_none_verdict_proceeds_normally(test_db):
    ws = FakeWebSocket([])
    clients = ScriptedClients(nlp=_nlp(), stt_text="hello")
    call = _call(ws, clients, verdict=None)

    await call._handle_audio(_audio_msg())  # noqa: SLF001
    customer = [
        t for t in ws.frames_of(ServerMessageType.transcript) if t["speaker"] == "customer"
    ]
    assert len(customer) == 1


async def test_no_pcm_skips_gate_entirely(test_db):
    ws = FakeWebSocket([])
    clients = ScriptedClients(nlp=_nlp(), stt_text="hello")
    call = _call(ws, clients, verdict=False)  # would hold IF the gate ran

    msg = ClientMessage(type=ClientMessageType.audio_chunk, audio_b64="x", is_final=True)
    await call._handle_audio(msg)  # noqa: SLF001
    customer = [
        t for t in ws.frames_of(ServerMessageType.transcript) if t["speaker"] == "customer"
    ]
    assert len(customer) == 1  # RMS-fallback clients keep working untouched
    assert call._smart_turn.calls == 0  # noqa: SLF001
