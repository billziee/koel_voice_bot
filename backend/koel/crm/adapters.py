"""Live-CRM adapter contract + generic signed-webhook mirror (6.1).

The dummy CRM's four operations ARE the interface a production CRM must satisfy
(``crm/schemas.py`` froze the request/response shapes); ``CrmAdapter`` states that
contract so a named connector (Salesforce/LeadSquared/CleverTap) is a drop-in.

Until a connector exists, ``mirror_to_webhook`` forwards each CRM write to a
client's middleware over an HMAC-signed webhook — the generic adapter that covers
most Indian lenders. It's best-effort and DORMANT until a URL + key are
configured, so the internal DB-backed CRM remains the source of truth and nothing
egresses with empty keys.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Protocol

from koel.common.config import CommonSettings, get_common_settings
from koel.common.http import make_async_client
from koel.common.logging import get_logger
from koel.crm.schemas import CallSummaryRequest, CustomerRecord, FlagRequest, InteractionLog

logger = get_logger("koel.crm.adapters")


class CrmAdapter(Protocol):
    """The interface a production CRM integration must satisfy (6.1)."""

    async def get_customer(self, customer_id: str) -> CustomerRecord: ...
    async def log_interaction(self, customer_id: str, payload: InteractionLog) -> None: ...
    async def set_flag(self, customer_id: str, payload: FlagRequest) -> None: ...
    async def post_call_summary(self, customer_id: str, payload: CallSummaryRequest) -> None: ...


def _sign(key: str, raw: bytes) -> str:
    """HMAC-SHA256 of the body so the receiver can verify origin + integrity."""
    return "sha256=" + hmac.new(key.encode(), raw, hashlib.sha256).hexdigest()


async def mirror_to_webhook(
    event: str, customer_id: str, data: dict, *, settings: CommonSettings | None = None
) -> bool:
    """Forward a CRM write to the client's CRM webhook. Returns True on accepted
    delivery, False when dormant (unconfigured) or on failure. Never raises."""
    settings = settings or get_common_settings()
    if not (settings.crm_webhook_url and settings.crm_webhook_key):
        return False
    raw = json.dumps(
        {"event": event, "customer_id": customer_id, "data": data},
        separators=(",", ":"), sort_keys=True, default=str,
    ).encode()
    headers = {"Content-Type": "application/json", "X-Koel-Signature": _sign(settings.crm_webhook_key, raw)}
    try:
        async with make_async_client(timeout_s=settings.crm_webhook_timeout_s) as client:
            resp = await client.post(settings.crm_webhook_url, content=raw, headers=headers)
            resp.raise_for_status()
        logger.info("crm_mirror_sent event=%s", event)
        return True
    except Exception:  # noqa: BLE001 — mirroring is best-effort; the local write already succeeded
        logger.warning("crm_mirror_failed event=%s", event, exc_info=True)
        return False
