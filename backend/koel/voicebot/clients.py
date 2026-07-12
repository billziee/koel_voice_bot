"""HTTP clients VB_Svc uses to reach the AI microservices and the Dummy CRM.

Each call propagates the service-to-service access token and the correlation
id so OTel traces stitch FE → VB_Svc → AI svc into one trace (PRD §08).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from urllib.parse import quote

import httpx
from opentelemetry.propagate import inject

from koel.common.auth import issue_token
from koel.common.config import CommonSettings
from koel.common.http import make_async_client
from koel.common.logging import get_correlation_id
from koel.crm.schemas import CustomerRecord
from koel.nlp.schemas import NlpResponse, NlpStreamFrame, TurnContext
from koel.stt.schemas import TranscribeRequest, TranscriptResult
from koel.tts.schemas import SynthesisChunk, SynthesisResult, SynthesizeRequest
from koel.voicebot.tts_cache import TTS_CLIP_CACHE, TTS_RESULT_CACHE, tts_cache_key

_SERVICE_PRINCIPAL = "voicebot_svc"

# TTS providers (Orpheus, Magpie) each carry their own ~30s internal ceiling
# (groq_timeout_s / nvidia_tts_timeout_s) for a real neural-synthesis call, and
# on the non-streaming path a failed primary cascades into a second full
# attempt against the fallback. The generic 10s STT/NLP/CRM timeout used to
# apply here too, so the orchestrator's own client gave up and disconnected
# *before* the provider could finish or even fail — starving Magpie/Hindi
# turns of audio every time (the cancellation raises asyncio.CancelledError,
# a BaseException, so the provider's own except-Exception fallback never ran).
_TTS_STREAM_TIMEOUT_S = 40.0  # one provider attempt, no cascade on the stream path
_TTS_SYNTHESIZE_TIMEOUT_S = 70.0  # primary + fallback cascade, worst case
_WARMUP_TIMEOUT_S = 6.0  # session warm-up ping (O-1); short — it's best-effort
_NLP_STREAM_TIMEOUT_S = 15.0  # first-token NLP stream (O-2): generation ≲1s + margin


class ServiceClients:
    """Thin async clients with shared auth + tracing headers."""

    def __init__(self, settings: CommonSettings, *, timeout_s: float = 10.0) -> None:
        self._settings = settings
        self._client = make_async_client(timeout_s=timeout_s)

    def _headers(self) -> dict[str, str]:
        # Mint a fresh access token per fan-out; VB_Svc authenticates to peers.
        token = issue_token(_SERVICE_PRINCIPAL, kind="access", role="service", settings=self._settings)
        headers = {"Authorization": f"Bearer {token}"}
        cid = get_correlation_id()
        if cid:
            headers["x-correlation-id"] = cid
        # Inject W3C trace context (traceparent/tracestate) so the AI-service
        # spans stitch into the same distributed trace as this turn (PRD §08).
        inject(headers)
        return headers

    async def warm_up(self) -> None:
        """Prime the STT/NLP/TTS provider connection pools at session start (O-1)
        so the caller's first turn skips the cold cross-ocean TCP+TLS+HTTP/2
        handshake to Groq/Sarvam. Fire-and-forget and fully best-effort: every
        failure is swallowed, so a warm-up miss only means a slower first turn,
        never a broken call. The greeting synthesis already warms the TTS path;
        this closes the STT + NLP holes before the customer speaks.
        """
        targets = (
            self._settings.stt_svc_url,
            self._settings.nlp_svc_url,
            self._settings.tts_svc_url,
        )
        headers = self._headers()

        async def _warm(base: str) -> None:
            try:
                await self._client.post(
                    f"{base}/v1/warmup", headers=headers, timeout=_WARMUP_TIMEOUT_S
                )
            except Exception:  # noqa: BLE001 — warming is best-effort
                pass

        await asyncio.gather(*(_warm(b) for b in targets))

    # --- STT ---
    async def transcribe(self, request: TranscribeRequest) -> TranscriptResult:
        resp = await self._client.post(
            f"{self._settings.stt_svc_url}/v1/transcribe",
            json=request.model_dump(),
            headers=self._headers(),
        )
        resp.raise_for_status()
        return TranscriptResult.model_validate(resp.json())

    # --- NLP ---
    async def respond(self, ctx: TurnContext) -> NlpResponse:
        resp = await self._client.post(
            f"{self._settings.nlp_svc_url}/v1/respond",
            json=ctx.model_dump(mode="json"),
            headers=self._headers(),
        )
        resp.raise_for_status()
        return NlpResponse.model_validate(resp.json())

    async def respond_stream(self, ctx: TurnContext) -> AsyncIterator[NlpStreamFrame]:
        """Consume NLP_svc /v1/respond/stream (NDJSON) so the orchestrator can feed
        each reply sentence into sentence-streaming TTS as it's generated (O-2).

        Yields ``sentence`` frames as they arrive, then a terminal ``final`` frame
        carrying the full ``NlpResponse`` (intent/escalation/state). The batch
        ``respond()`` stays the fallback for the stub/non-streaming path and when
        ``stream_nlp`` is off.
        """
        async with self._client.stream(
            "POST",
            f"{self._settings.nlp_svc_url}/v1/respond/stream",
            json=ctx.model_dump(mode="json"),
            headers=self._headers(),
            timeout=_NLP_STREAM_TIMEOUT_S,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line.strip():
                    yield NlpStreamFrame.model_validate_json(line)

    # --- TTS ---
    async def synthesize(self, request: SynthesizeRequest) -> SynthesisResult:
        """Synthesize a whole clip (non-streaming path).

        Repeated scripted lines are served from an in-process cache (O-3), the
        whole-clip analogue of the stream_synthesis cache: on a hit we return the
        stored clip (no network, ~0ms) stamped for the current session; on a miss
        we synthesize and cache the result. session_id is delivery-only and
        excluded from the key, so the same line hits across calls and sessions.
        """
        key = tts_cache_key(request)
        cached = TTS_RESULT_CACHE.get(key)
        if cached is not None:
            return cached[0].model_copy(update={"session_id": request.session_id})
        resp = await self._client.post(
            f"{self._settings.tts_svc_url}/v1/synthesize",
            json=request.model_dump(mode="json"),
            headers=self._headers(),
            timeout=_TTS_SYNTHESIZE_TIMEOUT_S,
        )
        resp.raise_for_status()
        result = SynthesisResult.model_validate(resp.json())
        TTS_RESULT_CACHE.put(key, [result])
        return result

    async def stream_synthesis(self, request: SynthesizeRequest) -> AsyncIterator[SynthesisChunk]:
        """Consume TTS_svc /v1/stream (NDJSON chunks) so a turn is cancellable (D).

        The orchestrator can stop iterating on TTS_KILL to abort mid-utterance;
        the whole-clip ``synthesize`` path stays the default for engines (Orpheus/
        Magpie) that return a complete clip rather than a token stream.

        Repeated scripted lines are served from an in-process cache (O-3): on a
        hit we replay the stored chunks locally (no network, ~0ms); on a miss we
        stream from TTS_svc, accumulate the chunks, and cache the sequence ONLY if
        the caller drains it fully — a barge-in that stops iteration early leaves
        the generator closed before the store, so a truncated clip is never cached.
        """
        request = request.model_copy(update={"stream": True})
        key = tts_cache_key(request)
        cached = TTS_CLIP_CACHE.get(key)
        if cached is not None:
            for chunk in cached:
                # Stamp the current session id onto the replayed chunk (the cached
                # copy carries the originating session's id, which is delivery-only).
                yield chunk.model_copy(update={"session_id": request.session_id})
            return

        collected: list[SynthesisChunk] = []
        async with self._client.stream(
            "POST",
            f"{self._settings.tts_svc_url}/v1/stream",
            json=request.model_dump(mode="json"),
            headers=self._headers(),
            timeout=_TTS_STREAM_TIMEOUT_S,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line.strip():
                    chunk = SynthesisChunk.model_validate_json(line)
                    collected.append(chunk)
                    yield chunk
        # Reached only when the consumer iterated to completion (no early break /
        # barge-in) and the stream finished without raising → cache the full clip.
        TTS_CLIP_CACHE.put(key, collected)

    # --- CRM ---
    async def get_customer(self, customer_id: str) -> CustomerRecord:
        resp = await self._client.get(
            f"{self._settings.crm_svc_url}/v1/customers/{quote(customer_id, safe='')}",
            headers=self._headers(),
        )
        resp.raise_for_status()
        return CustomerRecord.model_validate(resp.json())

    async def get_customer_by_phone(
        self, phone: str, *, org_id: str | None = None
    ) -> CustomerRecord | None:
        """ANI lookup for inbound calls (roadmap 3.2): resolve a caller by their
        phone number. Returns ``None`` on a 404 (unknown ANI) so an unresolved
        caller is still served as an anonymous inbound session — the lookup never
        raises for a miss. Other HTTP errors still propagate. ``org_id`` scopes the
        lookup to one tenant (AF), mirroring the session's bot org."""
        params = {"phone": phone}
        if org_id is not None:
            params["org_id"] = org_id
        resp = await self._client.get(
            f"{self._settings.crm_svc_url}/v1/customers/by-phone",
            params=params,
            headers=self._headers(),
        )
        if resp.status_code == httpx.codes.NOT_FOUND:
            return None
        resp.raise_for_status()
        return CustomerRecord.model_validate(resp.json())

    async def flag_customer(self, customer_id: str, flag: str, reason: str | None = None) -> None:
        await self._client.post(
            f"{self._settings.crm_svc_url}/v1/customers/{quote(customer_id, safe='')}/flags",
            json={"customer_id": customer_id, "flag": flag, "reason": reason},
            headers=self._headers(),
        )

    async def add_call_summary(
        self, customer_id: str, *, session_id: str, interest_shown: bool,
        objection: str = "", sentiment: str = "neutral", next_action: str = "",
    ) -> None:
        await self._client.post(
            f"{self._settings.crm_svc_url}/v1/customers/{quote(customer_id, safe='')}/call-summary",
            json={
                "session_id": session_id, "interest_shown": interest_shown,
                "objection": objection, "sentiment": sentiment, "next_action": next_action,
            },
            headers=self._headers(),
        )

    async def aclose(self) -> None:
        await self._client.aclose()
