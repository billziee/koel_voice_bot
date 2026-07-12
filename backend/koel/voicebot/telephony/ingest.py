"""Server-side endpointing for continuous PSTN audio (roadmap 1.1 STT seam).

The browser path receives DISCRETE utterance blobs: the client runs its own VAD
and posts one ``audio_chunk`` per finished utterance with ``is_final=True``, so
``ws/handler._handle_audio`` can transcribe each blob directly. Telephony is
different — a carrier (Twilio Media Streams) streams a CONTINUOUS sequence of
~20 ms media frames with NO client-side VAD and NO end-of-utterance marker. So
*something server-side* has to decide where one caller utterance ends and the
next begins before STT can run.

:class:`TelephonyAudioIngest` is that decision. It sits on the bridge's
``on_caller_audio`` seam: every decoded PCM16 frame is appended to a buffer while
a simple energy (RMS) gate tracks trailing silence. When the caller has stopped
talking for long enough (and enough speech has accumulated), or a hard ceiling is
hit, the whole buffered utterance is flushed to the injected ``transcribe``
callback and, if that yields a final non-empty transcript, handed to
``on_utterance`` (the route wires this to ``orchestrator.handle_final_transcript``).

HONESTY / SCOPE (important): the ENDPOINTING STRUCTURE — buffer → detect trailing
silence → transcribe → hand the final transcript to the orchestrator — is what is
designed and unit-tested here. The RMS/SILENCE THRESHOLDS below are a documented
STARTING POINT only; the "right" ``silence_rms_threshold`` in particular depends
on the actual PSTN comfort-noise floor after µ-law→PCM decode and MUST be tuned
against real carrier audio before this is trusted in production. Everything is a
pure-ish state machine (no I/O except the two injected async callbacks) so the
structure is deterministic and testable without a carrier, STT, or Redis; it is
also fully defensive — a bad frame or a failing STT call is logged, never raised
into the live call.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from koel.common.logging import get_logger
from koel.stt.schemas import TranscriptResult

logger = get_logger("koel.voicebot.telephony.ingest")

# 16-bit mono PCM: two bytes per sample (matches codec.py's ``_PCM_WIDTH``).
_PCM_WIDTH = 2

# STT seam callbacks the route injects.
#  - ``transcribe`` wraps ``clients.transcribe`` (PCM16 + sample-rate → result);
#    may return ``None`` when the provider yields nothing usable.
#  - ``on_utterance`` receives a FINAL, non-empty transcript (→ the orchestrator).
TranscribeCallback = Callable[[bytes, int], Awaitable[TranscriptResult | None]]
UtteranceCallback = Callable[[TranscriptResult], Awaitable[None]]

# --- default thresholds (STARTING POINT — see module docstring) --------------
# RMS energy below which a frame is treated as silence. PCM16 amplitude runs
# 0..32767; this is a conservative floor that must be tuned to the live PSTN
# comfort-noise level after µ-law decode.
_DEFAULT_SILENCE_RMS_THRESHOLD = 500
# Trailing silence that marks end-of-utterance (a natural inter-turn gap).
_DEFAULT_TRAILING_SILENCE_MS = 600
# Ignore blips (a cough, a line click) shorter than this much *speech*.
_DEFAULT_MIN_UTTERANCE_MS = 200
# Hard ceiling: flush a monologue this long even without a silence gap, so a
# non-stop talker still gets transcribed and buffer growth stays bounded.
_DEFAULT_MAX_UTTERANCE_MS = 15000


@dataclass(frozen=True)
class EndpointingConfig:
    """Immutable tuning knobs for the silence-gated endpointer.

    Defaults are a documented starting point (see module docstring); the RMS
    threshold especially needs tuning against real PSTN audio.
    """

    silence_rms_threshold: int = _DEFAULT_SILENCE_RMS_THRESHOLD
    trailing_silence_ms: int = _DEFAULT_TRAILING_SILENCE_MS
    min_utterance_ms: int = _DEFAULT_MIN_UTTERANCE_MS
    max_utterance_ms: int = _DEFAULT_MAX_UTTERANCE_MS


class TelephonyAudioIngest:
    """Accumulates continuous caller PCM and flushes complete utterances to STT.

    Construct with a ``transcribe`` callback (PCM16 + sample-rate → result) and an
    ``on_utterance`` callback (final transcript → orchestrator); then feed decoded
    frames via :meth:`feed`. End-of-utterance is detected by trailing silence (or
    the hard ceiling); :meth:`flush` can also be called explicitly at stream stop
    to drain any residual speech.
    """

    def __init__(
        self,
        *,
        transcribe: TranscribeCallback,
        on_utterance: UtteranceCallback,
        silence_rms_threshold: int = _DEFAULT_SILENCE_RMS_THRESHOLD,
        trailing_silence_ms: int = _DEFAULT_TRAILING_SILENCE_MS,
        min_utterance_ms: int = _DEFAULT_MIN_UTTERANCE_MS,
        max_utterance_ms: int = _DEFAULT_MAX_UTTERANCE_MS,
    ) -> None:
        self._transcribe = transcribe
        self._on_utterance = on_utterance
        self._cfg = EndpointingConfig(
            silence_rms_threshold=silence_rms_threshold,
            trailing_silence_ms=trailing_silence_ms,
            min_utterance_ms=min_utterance_ms,
            max_utterance_ms=max_utterance_ms,
        )
        # Mutable state machine (a running buffer is inherently mutable).
        self._buffer = bytearray()
        self._sample_rate = 0  # locked from the first non-empty frame
        self._speech_ms = 0.0  # voiced audio accumulated in the buffer
        self._trailing_silence_ms = 0.0  # consecutive silence at the buffer tail

    @property
    def config(self) -> EndpointingConfig:
        """The (immutable) thresholds in force — handy for logging/tests."""
        return self._cfg

    async def feed(self, pcm16: bytes, sample_rate: int) -> None:
        """Append one decoded caller frame and flush if the utterance is complete.

        Voiced frames (RMS ≥ threshold) reset the trailing-silence timer and grow
        the speech total; silent frames extend the trailing-silence timer. A flush
        fires when trailing silence ≥ ``trailing_silence_ms`` AND accumulated
        speech ≥ ``min_utterance_ms``, OR when the buffer reaches
        ``max_utterance_ms``. Never raises into the caller.
        """
        try:
            if not pcm16:
                return
            if sample_rate > 0:
                self._sample_rate = sample_rate
            self._buffer.extend(pcm16)

            frame_ms = self._bytes_to_ms(len(pcm16))
            if self._rms(pcm16) >= self._cfg.silence_rms_threshold:
                self._trailing_silence_ms = 0.0
                self._speech_ms += frame_ms
            else:
                self._trailing_silence_ms += frame_ms

            buffered_ms = self._bytes_to_ms(len(self._buffer))
            end_of_utterance = (
                self._trailing_silence_ms >= self._cfg.trailing_silence_ms
                and self._speech_ms >= self._cfg.min_utterance_ms
            )
            if buffered_ms >= self._cfg.max_utterance_ms or end_of_utterance:
                await self.flush()
        except Exception:  # noqa: BLE001 — endpointing must never crash a live call
            logger.warning(
                "telephony_ingest_feed_failed",
                exc_info=True,
            )

    async def flush(self) -> None:
        """Transcribe the buffered utterance and hand a final transcript onward.

        No-op when the buffer holds less than ``min_utterance_ms`` of audio. The
        buffer is snapshotted and reset BEFORE the (awaited) transcribe call so a
        re-entrant :meth:`feed` starts a clean utterance. Called on detected
        end-of-utterance and again at stream stop. Never raises into the caller.
        """
        pcm16 = bytes(self._buffer)
        sample_rate = self._sample_rate
        buffered_ms = self._bytes_to_ms(len(pcm16))
        # Reset first: whatever happens next, this utterance is consumed.
        self._reset()

        if buffered_ms < self._cfg.min_utterance_ms:
            return

        try:
            result = await self._transcribe(pcm16, sample_rate)
        except Exception:  # noqa: BLE001 — an STT outage must not drop the call
            logger.warning("telephony_ingest_transcribe_failed", exc_info=True)
            return

        if result is None or not result.is_final or not result.text.strip():
            # Empty/partial/None → nothing worth a turn (silence, noise, provider miss).
            return

        try:
            await self._on_utterance(result)
        except Exception:  # noqa: BLE001 — a turn-handler failure is the route's to own
            logger.warning("telephony_ingest_on_utterance_failed", exc_info=True)

    # -- internals ------------------------------------------------------------

    def _reset(self) -> None:
        self._buffer = bytearray()
        self._speech_ms = 0.0
        self._trailing_silence_ms = 0.0

    def _bytes_to_ms(self, num_bytes: int) -> float:
        if self._sample_rate <= 0:
            return 0.0
        samples = num_bytes / _PCM_WIDTH
        return samples / self._sample_rate * 1000.0

    @staticmethod
    def _rms(pcm16: bytes) -> int:
        """Root-mean-square energy of a PCM16 frame; 0 on a malformed fragment."""
        # Localised import (like codec.py) so the py3.11 audioop deprecation stays contained.
        import audioop

        try:
            return audioop.rms(pcm16, _PCM_WIDTH)
        except audioop.error:
            return 0
