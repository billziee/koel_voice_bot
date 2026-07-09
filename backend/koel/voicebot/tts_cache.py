"""In-process TTS clip cache (roadmap O-3).

Scripted flows repeat lines — disclosures, consent asks, reprompts, common
objection responses — and each otherwise pays full neural synthesis (~300-800ms)
plus ocean RTT on every turn. This caches the *completed* chunk sequence for a
synthesized line and replays it locally on a hit.

Safety properties (this is the hot audio path):
- The key covers every field that changes the produced audio and excludes
  ``session_id``/``stream`` (delivery-only), so the same line hits across calls.
- Only whole, fully-streamed clips are stored — a barge-in-truncated partial is
  never cached (see ServiceClients.stream_synthesis).
- The cache is in-process, so a deploy/restart clears it; combined with the TTL,
  a stale clip can never outlive the process or the configured retention window.
  Synthesis for a fixed (text, voice, language, prosody) is effectively
  deterministic, so a replay is the same audio the provider would have returned.

Retention / PII (DPDP §11 data-minimisation): the key includes the RENDERED
spoken text, which can embed a customer's CRM name (e.g. "Rahul ji, ..."), so a
cached clip and its text are derived personal data. The TTL that bounds how long
that PII can sit in process memory is driven by ``CommonSettings.tts_cache_ttl_s``
(KOEL_TTS_CACHE_TTL_S, default 30 min) — see the singletons at the bottom of this
module — rather than a hardcoded multi-hour window.

Eviction is LRU, bounded by the total cached audio size (a rough memory cap).
For a multi-worker deployment this would move to Redis with the same key.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict

from koel.common.config import get_common_settings
from koel.tts.schemas import SynthesizeRequest


def tts_cache_key(request: SynthesizeRequest) -> str:
    """Stable key over the fields that determine the synthesized audio.

    Excludes ``session_id`` and ``stream`` (they do not change the bytes), so the
    same spoken line hits regardless of which call or transport produced it.
    """
    parts = (
        request.text,
        request.language.value,
        request.tone.value,
        request.voice_gender,
        f"{request.speaking_rate:.3f}",
        "1" if request.ambient else "0",
        request.ambient_profile or "",
        f"{request.ambient_gain:.3f}",
        "1" if request.telephony else "0",
    )
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


class TtsClipCache:
    """LRU cache of completed synthesis chunk sequences, bounded by audio bytes + TTL."""

    # Default TTL is a conservative 30 min (not the former multi-hour window) so a
    # bare instance is bounded even if constructed without settings; the process
    # singletons below override it from CommonSettings.tts_cache_ttl_s.
    def __init__(self, *, max_bytes: int = 64 * 1024 * 1024, ttl_s: float = 1800.0) -> None:
        self._max_bytes = max_bytes
        self._ttl_s = ttl_s
        # key -> (stored_at_monotonic, chunks, byte_size)
        self._entries: OrderedDict[str, tuple[float, list, int]] = OrderedDict()
        self._bytes = 0

    def _now(self, now: float | None) -> float:
        return time.monotonic() if now is None else now

    def get(self, key: str, *, now: float | None = None) -> list | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, chunks, _size = entry
        if self._now(now) - stored_at > self._ttl_s:
            self._drop(key)
            return None
        self._entries.move_to_end(key)  # mark most-recently-used
        return chunks

    def put(self, key: str, chunks: list, *, now: float | None = None) -> None:
        if not chunks:
            return  # never cache an empty result
        size = _chunks_bytes(chunks)
        if size > self._max_bytes:
            return  # a single clip bigger than the whole budget — skip it
        if key in self._entries:
            self._drop(key)
        self._entries[key] = (self._now(now), chunks, size)
        self._bytes += size
        while self._bytes > self._max_bytes and self._entries:
            oldest_key = next(iter(self._entries))
            self._drop(oldest_key)

    def _drop(self, key: str) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._bytes -= entry[2]

    def clear(self) -> None:
        self._entries.clear()
        self._bytes = 0

    def __len__(self) -> int:
        return len(self._entries)


def _chunks_bytes(chunks: list) -> int:
    # Base64 payload length is a good proxy for the memory a clip holds.
    return sum(len(getattr(c, "audio_b64", "") or "") for c in chunks)


# Process-wide singletons so identical lines hit across concurrent sessions.
#
# Retention is driven by config, not hardcoded (DPDP §11 data-minimisation): the
# cache key includes the rendered spoken text, which can embed a customer's CRM
# name (e.g. "Rahul ji, ..."), so a cached clip and its text are derived personal
# data. Bounding the TTL (KOEL_TTS_CACHE_TTL_S, default 30 min) caps how long that
# PII can sit in server memory cross-session — a much tighter window than the
# former multi-hour default — and a process restart clears it entirely.
_settings = get_common_settings()

TTS_CLIP_CACHE = TtsClipCache(
    max_bytes=_settings.tts_cache_max_bytes, ttl_s=_settings.tts_cache_ttl_s
)

# Companion cache for the non-streaming whole-clip path (ServiceClients.synthesize,
# used when stream_tts is off and for whole-clip engines). Kept as a SEPARATE
# instance from TTS_CLIP_CACHE on purpose: the cache key excludes ``stream``, so a
# chunk sequence and a whole-clip result for the same line collide on one key and
# must not share storage. The SynthesisResult is stored as a one-item list —
# _chunks_bytes reads ``.audio_b64``, which SynthesisResult also carries.
TTS_RESULT_CACHE = TtsClipCache(
    max_bytes=_settings.tts_cache_max_bytes, ttl_s=_settings.tts_cache_ttl_s
)
