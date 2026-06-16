"""HTTP clients VB_Svc uses to reach the AI microservices and the Dummy CRM.

Each call propagates the service-to-service access token and the correlation
id so OTel traces stitch FE → VB_Svc → AI svc into one trace (PRD §08).
"""

from __future__ import annotations

from urllib.parse import quote

import httpx

from koel.common.auth import issue_token
from koel.common.config import CommonSettings
from koel.common.logging import get_correlation_id
from koel.crm.schemas import CustomerRecord
from koel.nlp.schemas import NlpResponse, TurnContext
from koel.stt.schemas import TranscribeRequest, TranscriptResult
from koel.tts.schemas import SynthesisResult, SynthesizeRequest

_SERVICE_PRINCIPAL = "voicebot_svc"


class ServiceClients:
    """Thin async clients with shared auth + tracing headers."""

    def __init__(self, settings: CommonSettings, *, timeout_s: float = 10.0) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(timeout=timeout_s)

    def _headers(self) -> dict[str, str]:
        # Mint a fresh access token per fan-out; VB_Svc authenticates to peers.
        token = issue_token(_SERVICE_PRINCIPAL, kind="access", role="service", settings=self._settings)
        headers = {"Authorization": f"Bearer {token}"}
        cid = get_correlation_id()
        if cid:
            headers["x-correlation-id"] = cid
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
        )
        resp.raise_for_status()
        return SynthesisResult.model_validate(resp.json())

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

    async def aclose(self) -> None:
        await self._client.aclose()
