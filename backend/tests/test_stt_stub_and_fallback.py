"""Stub STT language detection + orchestrator unsupported-language escalation."""

from __future__ import annotations

import base64
import json

from koel.common.schemas import Language
from koel.stt.providers import StubWhisperProvider, _detect_language
from koel.stt.schemas import TranscribeRequest


def test_detect_language_variants():
    assert _detect_language("yes I am interested in the offer")[0] == Language.en
    assert _detect_language("haan mujhe theek lagta hai aap")[0] == Language.hinglish
    assert _detect_language("मुझे नहीं चाहिए")[0] == Language.hi
    assert _detect_language("வணக்கம்")[0] == Language.other  # unsupported → fallback


async def test_stub_demo_utterance_when_not_text_sentinel():
    # Non-sentinel audio bytes → a deterministic demo utterance (not a crash).
    r = await StubWhisperProvider().transcribe(TranscribeRequest(
        session_id="s", audio_b64=base64.b64encode(b"\x10\x20" * 40).decode(), is_final=True,
    ))
    assert r.text and r.language_id in (Language.en, Language.hi, Language.hinglish)


async def test_language_hint_overrides_detection():
    payload = base64.b64encode(b"TEXT:hello there").decode()
    r = await StubWhisperProvider().transcribe(TranscribeRequest(
        session_id="s", audio_b64=payload, is_final=True, language_hint=Language.hi,
    ))
    assert r.language_id == Language.hi  # hint wins


async def test_unsupported_language_escalates(test_db):
    # Orchestrator routes an out-of-EN/HI/Hinglish turn to a graceful handoff
    # (respond is never reached — FakeClients.respond raises if it were).
    from koel.stt.schemas import TranscriptResult
    from koel.voicebot.session.models import CallStatus
    from tests.conftest import FakeClients
    from tests.test_ws_session import FakeWebSocket, _session, _state

    class _Clients(FakeClients):
        async def transcribe(self, request):  # noqa: ANN001
            return TranscriptResult(
                session_id=request.session_id, text="வணக்கம்", is_final=True,
                language_id=Language.other, language_confidence=0.95,
                speech_duration_ms=600, word_confidence=0.9,
            )

    ws = FakeWebSocket([json.dumps({"type": "audio_chunk", "audio_b64": "x", "is_final": True})])
    state = _state("ws_unsupported")
    # Opt into the (now default-off) STT-language escalation to exercise it.
    call = _session(ws, state, _Clients(), escalate_on_unsupported_language=True)
    await call.run()
    # Escalated to a human on the unsupported-language branch.
    assert state.escalated is True or state.status in (CallStatus.escalating, CallStatus.ended)
