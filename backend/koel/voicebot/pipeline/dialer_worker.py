"""Outbound dialer background loop (1.2) — the process that actually drains
the campaign dial queue built by ``campaigns.py``/``pipeline/dialer.py``.

Started (conditionally — see :func:`dialer_enabled`) from ``main.py``'s
lifespan alongside the app's other background work, and cancelled (not
awaited to completion — it's intentionally infinite) on shutdown.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from koel.common.http import make_async_client
from koel.common.logging import get_logger, mask_account
from koel.crm.schemas import CustomerRecord
from koel.db.models import Campaign
from koel.db.session import session_scope
from koel.voicebot.campaigns import claim_due_attempts, record_dial_result
from koel.voicebot.pipeline.compliance import is_within_calling_hours
from koel.voicebot.pipeline.outbound_dialer import originate_call

logger = get_logger("koel.voicebot.dialer_worker")

_ORIGINATE_TIMEOUT_S = 10.0
# How long to sleep before re-checking calling hours when the loop finds itself
# outside the window — short enough that the very first in-hours poll picks
# the queue back up promptly, not the full ``poll_interval_s`` cadence.
_HOURS_RECHECK_INTERVAL_S = 60.0


class CrmLookup(Protocol):
    """The single CRM method the dialer needs — DND scrub before dialling
    (PRD §11), same contract ``inbound.py``'s ``CrmLookup`` uses."""

    async def get_customer(
        self, customer_id: str, *, org_id: str | None = None
    ) -> CustomerRecord | None: ...


class CallingHoursSettings(Protocol):
    enforce_calling_hours: bool
    calling_hours_tz: str
    calling_hours_start: int
    calling_hours_end: int


def dialer_enabled(settings) -> bool:  # noqa: ANN001 — VoicebotSettings (avoid import cycle)
    """Mirrors the fail-closed dormancy gate every other carrier integration in
    this codebase uses: all three of the account sid/auth token/from number
    (plus a public URL to build the Stream callback against) must be set."""
    return bool(
        settings.twilio_account_sid
        and settings.twilio_auth_token
        and settings.twilio_from_number
        and settings.public_base_url
    )


async def _bot_id_for(campaign_id: str | None, default_bot_id: str) -> str | None:
    """The campaign's own bot, or the configured inbound default for a
    campaign-less attempt (a scheduled callback, 2.5). ``None`` means there is
    genuinely no bot to bind — the caller must give up on the attempt rather
    than dial a customer into nothing."""
    if campaign_id is None:
        return default_bot_id or None
    async with session_scope() as db:
        campaign = await db.get(Campaign, campaign_id)
    return campaign.bot_id if campaign is not None else None


async def _dnd_scrubbed(attempt: dict, crm: CrmLookup) -> bool:
    """True iff this attempt must NOT be dialled — the customer is on the DND
    registry (PRD §11). A lookup failure or missing customer_id degrades to
    "not DND" (dial proceeds) rather than silently starving the campaign on a
    transient CRM outage — the HTTP-triggered path (api/sessions.py) has CRM
    reachability as a harder dependency; the worker's queue is more tolerant."""
    if not attempt["customer_id"]:
        return False
    try:
        customer = await crm.get_customer(attempt["customer_id"], org_id=attempt["org_id"])
    except Exception:  # noqa: BLE001 — never stall the whole batch on one lookup
        logger.warning(
            "dialer_dnd_lookup_failed",
            extra={"extra_fields": {"attempt_id": attempt["id"]}},
            exc_info=True,
        )
        return False
    return customer is not None and customer.dnd


async def run_dialer_loop(
    *,
    account_sid: str,
    auth_token: str,
    from_number: str,
    public_base_url: str,
    default_bot_id: str,
    crm: CrmLookup,
    calling_hours: CallingHoursSettings,
    poll_interval_s: float = 30.0,
    batch_size: int = 10,
) -> None:
    """Claim due attempts and originate a call for each, forever, until
    cancelled. Every failure mode (claim error, missing bot, DND, origination
    error) is caught and handled per-attempt so one bad row never kills the
    loop or blocks the rest of the batch."""
    client = make_async_client(timeout_s=_ORIGINATE_TIMEOUT_S, http2=False)
    try:
        while True:
            if not is_within_calling_hours(
                enforce=calling_hours.enforce_calling_hours,
                tz_name=calling_hours.calling_hours_tz,
                start_hour=calling_hours.calling_hours_start,
                end_hour=calling_hours.calling_hours_end,
            ):
                # Outside the RBI fair-practice window (PRD §11): leave every
                # due row untouched (still `pending`, not claimed/retried) and
                # recheck soon rather than waiting a full poll cycle.
                await asyncio.sleep(_HOURS_RECHECK_INTERVAL_S)
                continue
            try:
                attempts = await claim_due_attempts(limit=batch_size)
            except Exception:  # noqa: BLE001 — a claim failure must not kill the loop
                logger.warning("dialer_claim_failed", exc_info=True)
                attempts = []
            for attempt in attempts:
                if await _dnd_scrubbed(attempt, crm):
                    logger.info(
                        "dialer_attempt_dnd_scrubbed",
                        extra={"extra_fields": {
                            "attempt_id": attempt["id"], "phone": mask_account(attempt["phone"]),
                        }},
                    )
                    await record_dial_result(attempt["id"], "dnd")
                    continue
                bot_id = await _bot_id_for(attempt["campaign_id"], default_bot_id)
                if bot_id is None:
                    logger.warning(
                        "dialer_attempt_has_no_bot",
                        extra={"extra_fields": {"attempt_id": attempt["id"]}},
                    )
                    await record_dial_result(attempt["id"], "failed")
                    continue
                await originate_call(
                    attempt,
                    bot_id=bot_id,
                    account_sid=account_sid,
                    auth_token=auth_token,
                    from_number=from_number,
                    public_base_url=public_base_url,
                    client=client,
                )
            await asyncio.sleep(poll_interval_s)
    finally:
        await client.aclose()
