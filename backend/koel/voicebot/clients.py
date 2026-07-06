"""HTTP clients VB_Svc uses to reach the AI microservices and the Dummy CRM.

Each call propagates the service-to-service access token and the correlation
id so OTel traces stitch FE → VB_Svc → AI svc into one trace (PRD §08).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import quote

from opentelemetry.propagate import inject

from koel.common.auth import issue_token
from koel.common.config import CommonSettings
from koel.common.http import make_async_client
from koel.common.logging import get_correlation_id
from koel.crm.schemas import CustomerRecord
from koel.nlp.schemas import NlpResponse, TurnContext
from koel.stt.schemas import TranscribeRequest, TranscriptResult
from koel.tts.schemas import SynthesisChunk, SynthesisResult, SynthesizeRequest

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

    # --- TTS ---
    async def synthesize(self, request: SynthesizeRequest) -> SynthesisResult:
        resp = await self._client.post(
            f"{self._settings.tts_svc_url}/v1/synthesize",
            json=request.model_dump(mode="json"),
            headers=self._headers(),
            timeout=_TTS_SYNTHESIZE_TIMEOUT_S,
        )
        resp.raise_for_status()
        return SynthesisResult.model_validate(resp.json())

    async def stream_synthesis(self, request: SynthesizeRequest) -> AsyncIterator[SynthesisChunk]:
        """Consume TTS_svc /v1/stream (NDJSON chunks) so a turn is cancellable (D).

        The orchestrator can stop iterating on TTS_KILL to abort mid-utterance;
        the whole-clip ``synthesize`` path stays the default for engines (Orpheus/
        Magpie) that return a complete clip rather than a token stream.
        """
        request = request.model_copy(update={"stream": True})
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
                    yield SynthesisChunk.model_validate_json(line)

    # --- CRM ---
    async def get_customer(self, customer_id: str) -> CustomerRecord:
        resp = await self._client.get(
            f"{self._settings.crm_svc_url}/v1/customers/{quote(customer_id, safe='')}",
            headers=self._headers(),
        )
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
