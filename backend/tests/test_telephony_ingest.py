"""Server-side endpointing state machine (roadmap 1.1 STT seam).

Drives :class:`TelephonyAudioIngest` with synthetic PCM16 frames — loud
(constant-amplitude) frames read as speech, all-zero frames as silence — and a
stub ``transcribe``. Fully deterministic: the flush decision is a pure function
of the frames fed, so there are NO sleeps and no real STT/carrier.

Note: what's verified here is the STRUCTURE (buffer → trailing-silence detection
→ transcribe → hand-off), driven by explicit thresholds. The default RMS/silence
thresholds themselves are a starting point needing live PSTN tuning (see the
module docstring of ingest.py).
"""

from __future__ import annotations

import struct

from koel.stt.schemas import TranscriptResult
from koel.voicebot.telephony.ingest import TelephonyAudioIngest

_RATE = 16000
_FRAME_MS = 20  # a Twilio media frame is ~20 ms


def _loud_frame(ms: int = _FRAME_MS, rate: int = _RATE, amplitude: int = 8000) -> bytes:
    """A constant-amplitude PCM16 frame — RMS == amplitude, i.e. clearly voiced."""
    samples = int(rate * ms / 1000)
    return struct.pack(f"<{samples}h", *([amplitude] * samples))


def _silent_frame(ms: int = _FRAME_MS, rate: int = _RATE) -> bytes:
    """An all-zero PCM16 frame — RMS 0, i.e. silence."""
    samples = int(rate * ms / 1000)
    return b"\x00\x00" * samples


def _final(text: str = "haan boliye") -> TranscriptResult:
    return TranscriptResult(session_id="tw_ingest", text=text, is_final=True)


class StubTranscribe:
    """Records (pcm, sample_rate) it was called with; returns a preset result."""

    def __init__(self, result: TranscriptResult | None) -> None:
        self._result = result
        self.calls: list[tuple[bytes, int]] = []

    async def __call__(self, pcm16: bytes, sample_rate: int) -> TranscriptResult | None:
        self.calls.append((pcm16, sample_rate))
        return self._result


class UtteranceRecorder:
    """Records final transcripts handed to on_utterance."""

    def __init__(self) -> None:
        self.received: list[TranscriptResult] = []

    async def __call__(self, stt: TranscriptResult) -> None:
        self.received.append(stt)


async def test_flush_fires_after_trailing_silence_with_min_length_utterance():
    stt = StubTranscribe(_final("haan boliye"))
    rec = UtteranceRecorder()
    ingest = TelephonyAudioIngest(
        transcribe=stt,
        on_utterance=rec,
        trailing_silence_ms=100,
        min_utterance_ms=40,
        silence_rms_threshold=500,
    )

    # 60 ms of speech (>= 40 ms min) then a natural pause.
    for _ in range(3):
        await ingest.feed(_loud_frame(), _RATE)
    assert rec.received == []  # nothing flushed while the caller is still talking

    # 100 ms of trailing silence (== threshold) → end-of-utterance.
    for _ in range(5):
        await ingest.feed(_silent_frame(), _RATE)

    assert len(rec.received) == 1
    # on_utterance received exactly the stub's transcript…
    assert rec.received[0].text == "haan boliye"
    # …and transcribe saw the buffered PCM at the caller's sample rate.
    assert len(stt.calls) == 1
    pcm, rate = stt.calls[0]
    assert rate == _RATE
    assert len(pcm) > 0


async def test_short_blip_does_not_flush():
    stt = StubTranscribe(_final())
    rec = UtteranceRecorder()
    ingest = TelephonyAudioIngest(
        transcribe=stt,
        on_utterance=rec,
        trailing_silence_ms=100,
        min_utterance_ms=200,  # require 200 ms of speech
        silence_rms_threshold=500,
    )

    # A single 20 ms blip of speech — well under the 200 ms minimum.
    await ingest.feed(_loud_frame(), _RATE)
    # Then a long silence that far exceeds the trailing-silence threshold.
    for _ in range(20):
        await ingest.feed(_silent_frame(), _RATE)

    # Too little speech accumulated → the endpointer never flushed the blip.
    assert rec.received == []
    assert stt.calls == []


async def test_max_duration_cap_forces_a_flush():
    stt = StubTranscribe(_final("bahut lambi baat"))
    rec = UtteranceRecorder()
    ingest = TelephonyAudioIngest(
        transcribe=stt,
        on_utterance=rec,
        # No practical silence gap; a hard ceiling must still flush a monologue.
        trailing_silence_ms=10_000,
        min_utterance_ms=40,
        max_utterance_ms=100,
        silence_rms_threshold=500,
    )

    # 100 ms of uninterrupted speech reaches the ceiling exactly on the 5th frame.
    for _ in range(5):
        await ingest.feed(_loud_frame(), _RATE)

    assert len(rec.received) == 1
    assert rec.received[0].text == "bahut lambi baat"


async def test_none_transcribe_result_does_not_emit_an_utterance():
    stt = StubTranscribe(None)  # provider returned nothing usable
    rec = UtteranceRecorder()
    ingest = TelephonyAudioIngest(
        transcribe=stt, on_utterance=rec, trailing_silence_ms=100, min_utterance_ms=40
    )

    for _ in range(3):
        await ingest.feed(_loud_frame(), _RATE)
    for _ in range(5):
        await ingest.feed(_silent_frame(), _RATE)

    # transcribe WAS attempted, but a None result yields no turn.
    assert len(stt.calls) == 1
    assert rec.received == []


async def test_empty_text_transcript_does_not_emit_an_utterance():
    stt = StubTranscribe(TranscriptResult(session_id="tw_ingest", text="   ", is_final=True))
    rec = UtteranceRecorder()
    ingest = TelephonyAudioIngest(
        transcribe=stt, on_utterance=rec, trailing_silence_ms=100, min_utterance_ms=40
    )

    for _ in range(3):
        await ingest.feed(_loud_frame(), _RATE)
    for _ in range(5):
        await ingest.feed(_silent_frame(), _RATE)

    assert len(stt.calls) == 1
    assert rec.received == []  # blank/whitespace-only transcript is not a turn


async def test_non_final_transcript_does_not_emit_an_utterance():
    stt = StubTranscribe(TranscriptResult(session_id="tw_ingest", text="partial", is_final=False))
    rec = UtteranceRecorder()
    ingest = TelephonyAudioIngest(
        transcribe=stt, on_utterance=rec, trailing_silence_ms=100, min_utterance_ms=40
    )

    for _ in range(3):
        await ingest.feed(_loud_frame(), _RATE)
    for _ in range(5):
        await ingest.feed(_silent_frame(), _RATE)

    assert rec.received == []  # only final transcripts become turns


async def test_explicit_flush_drains_residual_speech_at_stream_stop():
    stt = StubTranscribe(_final("aakhri baat"))
    rec = UtteranceRecorder()
    ingest = TelephonyAudioIngest(
        transcribe=stt, on_utterance=rec, trailing_silence_ms=10_000, min_utterance_ms=40
    )

    # Speech with no trailing-silence gap: feed() never auto-flushes…
    for _ in range(4):
        await ingest.feed(_loud_frame(), _RATE)
    assert rec.received == []

    # …but an explicit flush (as the route calls at stream stop) drains it.
    await ingest.flush()
    assert len(rec.received) == 1

    # A second flush on the now-empty buffer is a harmless no-op.
    await ingest.flush()
    assert len(rec.received) == 1
