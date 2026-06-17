"""Regression: the no-input timer must start only after the bot's TTS audio
finishes playing — not when the customer last spoke (PRD §05)."""

from __future__ import annotations

import time

from koel.common.schemas import Language
from koel.voicebot.config import VoiceBotSettings
from koel.voicebot.session.models import SessionState
from koel.voicebot.ws.handler import CallSession
from koel.voicebot.ws.protocol import ServerMessage, ServerMessageType
from tests.conftest import FakeClients, FakeStore


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, s: str) -> None:
        self.sent.append(s)


def _call() -> CallSession:
    state = SessionState(session_id="s", bot_id="b", use_case="credit_card", language=Language.hinglish)
    return CallSession(
        websocket=_FakeWS(),
        state=state,
        settings=VoiceBotSettings(),
        store=FakeStore(),
        orchestrator=None,
        bargein=None,
        clients=FakeClients(),
        resumed=False,
    )


async def test_speech_end_pushes_the_no_input_baseline_forward():
    call = _call()
    before = time.monotonic()
    # Bot speaks an 8s clip — longer than the 5s no-input window.
    await call.send(
        ServerMessage.of(ServerMessageType.audio_chunk, audio_b64="", duration_ms=8000, index=0)
    )
    # The no-input clock is pinned to when that audio finishes (~+8s), so the
    # 5s timer cannot trip mid-sentence.
    assert call._speech_until >= before + 7.9  # noqa: SLF001
    baseline = max(call._last_input_at, call._speech_until)  # noqa: SLF001
    assert baseline == call._speech_until  # speech end dominates the stale input time  # noqa: SLF001
    remaining = (baseline + call._settings.no_input_reprompt_ms / 1000) - time.monotonic()  # noqa: SLF001
    assert remaining > 8.0  # ~8s of playback still ahead + the 5s window


async def test_zero_duration_audio_does_not_delay_timer():
    call = _call()
    await call.send(
        ServerMessage.of(ServerMessageType.audio_chunk, audio_b64="", duration_ms=0, index=0)
    )
    # No playback → baseline is effectively "now"; the window starts immediately.
    assert call._speech_until <= time.monotonic() + 0.05  # noqa: SLF001
