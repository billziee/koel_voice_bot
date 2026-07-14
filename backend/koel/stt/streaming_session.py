"""Long-lived Deepgram realtime STT session (O-6 Wave 2 — true continuous partials).

O-6 Wave 1 (``DeepgramSttProvider._stream_ws``) opens a Deepgram WS PER buffered
utterance: one WAV blob in → interim partials of *that blob* → one final → close.
That already streams partials, but only after the whole utterance has been
buffered and endpointed server-side, so the "streaming" is really per-turn.

This session is the Wave 2 upgrade: ONE long-lived Deepgram ``/v1/listen`` socket
that stays open across a whole turn (or call) while raw ``linear16`` PCM frames
are PUSHED as they arrive. With ``interim_results=true`` Deepgram emits
``is_final:false`` partials live as audio streams in, then an ``is_final:true``
(``speech_final``) final at the utterance boundary — genuine real-time STT with no
server-side buffering/endpointing on this path.

Protocol (verified live 2026-07, reused from :class:`DeepgramSttProvider`): binary
PCM frames in; JSON ``{"type":"Results","channel":{"alternatives":[{"transcript"}]},
"is_final":bool,"speech_final":bool}`` out; ``{"type":"CloseStream"}`` flushes; a
trailing ``{"type":"Metadata"}`` marks end-of-stream. Endpointing is delegated to
Deepgram (``is_final``/``speech_final``) — no RMS gate on this path.

DEFENSIVE BY CONTRACT: every WS error ends the session cleanly and is logged —
:meth:`send_frame`, :meth:`finish` and :meth:`aclose` never raise into the caller.
Only :meth:`start` may raise (a connect failure), so the caller can fall back to
the buffered ingest BEFORE any audio flows. Once started, the session degrades
silently (partials just stop) rather than dropping the live call.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from koel.common.logging import get_logger
from koel.common.schemas import Language
from koel.stt.schemas import TranscriptResult

logger = get_logger("koel.stt.streaming_session")

# Deepgram realtime listen path (nova-3). Same contract as DeepgramSttProvider.
_DEEPGRAM_WS_PATH = "/v1/listen"

# 16-bit linear PCM: two bytes per sample.
_PCM_WIDTH = 2

# Open the socket within this window or fail (→ caller falls back to buffered).
_DEFAULT_OPEN_TIMEOUT_S = 10.0
# Idle ceiling on a single ``recv``; also bounds how long ``finish`` waits for the
# trailing final/Metadata after ``CloseStream`` before it gives up and cancels.
_DEFAULT_RECV_TIMEOUT_S = 30.0
# Batch ~20 ms carrier frames up to ~100 ms so we send ~10 binary frames/sec
# instead of ~50 — enough to keep partials live without per-frame WS churn.
_DEFAULT_BATCH_MS = 100

# A partial receives the interim transcript text; a final receives a full result.
PartialCallback = Callable[[str], Awaitable[None]]
FinalCallback = Callable[[TranscriptResult], Awaitable[None]]


class DeepgramStreamingSession:
    """One long-lived Deepgram ``/v1/listen`` socket fed continuous PCM frames.

    Lifecycle: :meth:`start` (open WS + spawn the reader) → many
    :meth:`send_frame` → :meth:`finish` (flush + drain the last final) →
    :meth:`aclose`. Interim results are delivered to ``on_partial`` and finals to
    ``on_final`` by a background reader task, so partials surface the instant
    Deepgram emits them rather than at end-of-utterance.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "nova-3",
        base_url: str = "https://api.deepgram.com",
        sample_rate: int,
        on_partial: PartialCallback | None,
        on_final: FinalCallback,
        language: Language = Language.en,
        session_id: str = "",
        channels: int = 1,
        punctuate: bool = True,
        open_timeout_s: float = _DEFAULT_OPEN_TIMEOUT_S,
        recv_timeout_s: float = _DEFAULT_RECV_TIMEOUT_S,
        batch_ms: int = _DEFAULT_BATCH_MS,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._sample_rate = sample_rate
        self._on_partial = on_partial
        self._on_final = on_final
        self._language = language
        self._session_id = session_id
        self._open_timeout_s = open_timeout_s
        self._recv_timeout_s = recv_timeout_s
        self._batch_bytes = max(1, int(sample_rate * _PCM_WIDTH * batch_ms / 1000))
        # https:// → wss:// on the same host; encoding/sample_rate ride the query.
        ws_base = base_url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
        self._ws_url = (
            f"{ws_base}{_DEEPGRAM_WS_PATH}?model={model}&encoding=linear16"
            f"&sample_rate={sample_rate}&channels={channels}"
            f"&interim_results=true&punctuate={'true' if punctuate else 'false'}"
        )
        # Runtime state (a live socket + running reader are inherently mutable).
        self._ws: Any = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending = bytearray()
        self._started = False
        self._closed = False

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        """Open the Deepgram WS and spawn the background reader.

        Raises on a connect failure so the caller can fall back to the buffered
        ingest before any audio flows. After this returns the session is live and
        every other method is non-raising.
        """
        from websockets.asyncio.client import connect as ws_connect

        self._ws = await ws_connect(
            self._ws_url,
            additional_headers={"Authorization": f"Token {self._api_key}"},
            open_timeout=self._open_timeout_s,
        )
        self._started = True
        self._reader_task = asyncio.create_task(self._read_loop())

    async def send_frame(self, pcm16: bytes) -> None:
        """Buffer one PCM16 frame; flush to Deepgram once ~``batch_ms`` accumulated.

        Never raises: a send failure closes the session (partials stop) rather than
        surfacing into the live call.
        """
        if self._closed or self._ws is None or not pcm16:
            return
        self._pending.extend(pcm16)
        if len(self._pending) >= self._batch_bytes:
            try:
                await self._flush_pending()
            except Exception:  # noqa: BLE001 — a WS send error ends the session, not the call
                logger.warning(
                    "deepgram_stream_send_failed",
                    extra={"extra_fields": {"session_id": self._session_id}},
                    exc_info=True,
                )
                self._closed = True

    async def finish(self) -> None:
        """Flush buffered audio, ``CloseStream`` to finalize, drain the last final.

        Sends any batched remainder, tells Deepgram no more audio is coming, then
        awaits the reader draining the trailing final(s) + Metadata (bounded by
        ``recv_timeout_s`` so a silent upstream can't hang teardown) and stops it.
        Idempotent and never raises.
        """
        if self._closed:
            return
        try:
            await self._flush_pending()
            if self._ws is not None:
                await self._ws.send(json.dumps({"type": "CloseStream"}))
        except Exception:  # noqa: BLE001 — best-effort flush; still drain + close below
            logger.warning(
                "deepgram_stream_finish_failed",
                extra={"extra_fields": {"session_id": self._session_id}},
                exc_info=True,
            )
        # Await the reader draining the final(s) after CloseStream; wait_for cancels
        # it on timeout, so either way the reader is stopped when this returns.
        if self._reader_task is not None:
            try:
                await asyncio.wait_for(self._reader_task, timeout=self._recv_timeout_s)
            except (TimeoutError, asyncio.CancelledError):
                pass
            except Exception:  # noqa: BLE001 — a reader crash is already logged internally
                pass
        await self.aclose()

    async def aclose(self) -> None:
        """Stop the reader and close the socket, idempotently. Never raises."""
        self._closed = True
        await self._cancel_reader()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001 — teardown must never raise
                pass
            self._ws = None

    # -- internals ------------------------------------------------------------

    async def _flush_pending(self) -> None:
        if not self._pending or self._ws is None:
            return
        chunk = bytes(self._pending)
        self._pending = bytearray()
        await self._ws.send(chunk)

    async def _read_loop(self) -> None:
        """Consume Deepgram frames until Metadata / close / idle, dispatching
        partials + finals. A crash is logged, never propagated (it runs detached)."""
        from websockets.exceptions import ConnectionClosed

        try:
            while True:
                try:
                    frame = await asyncio.wait_for(self._ws.recv(), timeout=self._recv_timeout_s)
                except (ConnectionClosed, TimeoutError):
                    return  # socket closed after flush, or idle ceiling — stop cleanly
                if await self._handle_frame(frame):
                    return
        except asyncio.CancelledError:
            raise  # finish()/aclose() cancelled us — propagate for clean teardown
        except Exception:  # noqa: BLE001 — a reader crash must never surface into the call
            logger.warning(
                "deepgram_stream_reader_failed",
                extra={"extra_fields": {"session_id": self._session_id}},
                exc_info=True,
            )

    async def _handle_frame(self, frame: str | bytes) -> bool:
        """Handle one Deepgram frame; return True to end the read loop."""
        try:
            msg = json.loads(frame)
        except (ValueError, TypeError):
            return False  # non-JSON frame → skip, keep reading
        mtype = msg.get("type")
        if mtype == "Results":
            await self._dispatch_results(msg)
            return False
        if mtype == "Metadata":
            return True  # end-of-stream marker after CloseStream
        if mtype in ("Error", "error"):
            logger.warning(
                "deepgram_stream_error",
                extra={"extra_fields": {"session_id": self._session_id, "msg": str(msg)[:200]}},
            )
            return True
        return False

    async def _dispatch_results(self, msg: dict) -> None:
        alts = (msg.get("channel") or {}).get("alternatives") or []
        alt = alts[0] if alts else {}
        text = (alt.get("transcript") or "").strip()
        if not text:
            return  # empty interim (silence) → nothing to surface
        # Deepgram (default endpointing) marks utterance boundaries with is_final /
        # speech_final; everything else is a live interim. Mirrors the verified
        # DeepgramSttProvider semantics exactly.
        is_final = bool(msg.get("is_final")) or bool(msg.get("speech_final"))
        if is_final:
            await self._safe_final(self._build_result(msg, alt, text))
        else:
            await self._safe_partial(text)

    def _build_result(self, msg: dict, alt: dict, text: str) -> TranscriptResult:
        confidence = alt.get("confidence")
        conf = float(confidence) if confidence is not None else 0.9
        duration = msg.get("duration")
        duration_ms = int(float(duration) * 1000) if duration else 0
        # nova-3 carries no language id, so carry the pinned bot language through
        # (English-centric model → en default). The authoritative escalation
        # signal still comes from the batch fallback, not these partials.
        return TranscriptResult(
            session_id=self._session_id,
            text=text,
            is_final=True,
            language_id=self._language,
            language_confidence=conf,
            speech_duration_ms=duration_ms or max(120, len(text) * 60),
            word_confidence=conf,
        )

    async def _safe_partial(self, text: str) -> None:
        if self._on_partial is None:
            return
        try:
            await self._on_partial(text)
        except Exception:  # noqa: BLE001 — a partial-sink failure must not kill the reader
            logger.warning(
                "deepgram_stream_partial_sink_failed",
                extra={"extra_fields": {"session_id": self._session_id}},
                exc_info=True,
            )

    async def _safe_final(self, result: TranscriptResult) -> None:
        try:
            await self._on_final(result)
        except Exception:  # noqa: BLE001 — a turn-handler failure is the caller's to own
            logger.warning(
                "deepgram_stream_final_sink_failed",
                extra={"extra_fields": {"session_id": self._session_id}},
                exc_info=True,
            )

    async def _cancel_reader(self) -> None:
        task = self._reader_task
        self._reader_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — reader errors already handled internally
                pass
