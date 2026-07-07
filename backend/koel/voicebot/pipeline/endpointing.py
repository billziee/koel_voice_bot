"""Semantic end-of-turn detection — smart-turn v3.2 (Pipecat's ONNX model).

The frontend VAD ends an utterance after a fixed trailing-silence window, which
cuts off Hinglish speakers who pause mid-thought ("main soch raha tha ki …").
This gate asks a small Whisper-encoder ONNX model "is the speaker actually
done?" on the utterance audio. If not, the ws handler holds the transcript for
a short merge window and combines it with the follow-up utterance instead of
answering mid-thought.

Offline eval on Sarvam-TTS Hindi/Hinglish clips (docs/PIPECAT_UPGRADE_PLAN.md
Phase 4): 9/9 on production-representative cases — complete sentences scored
0.77-0.99, conjunction-ending fragments 0.005-0.027.

The model file is NOT committed (8.3MB) — scripts/fetch_smart_turn_model.sh
downloads it. Everything degrades gracefully: missing model, missing
onnxruntime, or any inference error simply disables the gate (is_complete
returns None → the handler proceeds normally, exactly as with the flag off).
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

from koel.common.logging import get_logger

logger = get_logger("koel.voicebot.endpointing")

_MODEL_SAMPLE_RATE = 16000
_WINDOW_SECONDS = 8  # model input: last 8s, front-padded with silence


def merge_transcripts(previous: str, followup: str) -> str:
    """Combine a held mid-thought fragment with its follow-up utterance."""
    return f"{previous.strip()} {followup.strip()}".strip()


class SmartTurnAnalyzer:
    """Lazy, self-disabling wrapper around the smart-turn-v3.2 ONNX model.

    Inference runs in a worker thread (~50-200ms CPU) so the event loop —
    heartbeats, barge-in — never stalls behind it.
    """

    def __init__(self, *, model_path: str, threshold: float = 0.5) -> None:
        self._model_path = Path(model_path)
        self._threshold = threshold
        self._session = None
        self._disabled = False

    def _ensure_session(self) -> bool:
        if self._session is not None:
            return True
        if self._disabled:
            return False
        try:
            import onnxruntime as ort

            if not self._model_path.is_file():
                raise FileNotFoundError(
                    f"{self._model_path} — run scripts/fetch_smart_turn_model.sh"
                )
            opts = ort.SessionOptions()
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 1
            self._session = ort.InferenceSession(
                str(self._model_path), providers=["CPUExecutionProvider"], sess_options=opts
            )
            logger.info("smart_turn_model_loaded path=%s", self._model_path)
            return True
        except Exception:  # noqa: BLE001 — degrade to "gate off", never break calls
            self._disabled = True
            logger.warning("smart_turn_unavailable; endpoint gate disabled", exc_info=True)
            return False

    async def is_complete(self, pcm16_b64: str) -> bool | None:
        """Classify an utterance (s16le mono 16kHz, base64) as a finished turn.

        Returns None when the gate can't run (model missing, bad audio) — the
        caller must treat None as "proceed normally".
        """
        return await asyncio.to_thread(self._predict, pcm16_b64)

    def _predict(self, pcm16_b64: str) -> bool | None:
        if not self._ensure_session():
            return None
        try:
            import numpy as np

            from koel.voicebot.pipeline.whisper_features import (
                compute_whisper_log_mel_features,
            )

            pcm = np.frombuffer(base64.b64decode(pcm16_b64), dtype=np.int16)
            if len(pcm) == 0:
                return None
            audio = pcm.astype(np.float32) / 32768.0
            max_samples = _WINDOW_SECONDS * _MODEL_SAMPLE_RATE
            if len(audio) > max_samples:
                audio = audio[-max_samples:]
            else:
                audio = np.pad(audio, (max_samples - len(audio), 0))
            features = compute_whisper_log_mel_features(audio, do_normalize=True)
            outputs = self._session.run(None, {"input_features": np.expand_dims(features, 0)})
            probability = float(outputs[0][0].item())
            complete = probability > self._threshold
            logger.debug("smart_turn prob=%.3f complete=%s", probability, complete)
            return complete
        except Exception:  # noqa: BLE001
            logger.warning("smart_turn_inference_failed", exc_info=True)
            return None
