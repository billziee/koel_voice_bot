"""Outbound call origination (1.2) — the half of the dialer that was dormant.

``campaigns.py`` claims due :class:`~koel.db.models.DialAttempt` rows;
``pipeline/dialer.py`` decides retry timing; this module is the missing piece
between them — actually placing the PSTN call via Twilio's REST Calls API and
pointing it at the already-built ``/twilio/stream`` bridge, then mapping the
call's eventual outcome back into ``record_dial_result`` so the retry policy
can act on it.

Dormant like every other carrier integration in this codebase: with no
``twilio_account_sid``/``twilio_auth_token``/``twilio_from_number`` configured,
``originate_call`` is a no-op. No vendor SDK — this project deliberately talks
to every provider (Groq, NVIDIA, Sarvam, Deepgram, Twilio) over plain httpx,
never an SDK, so this follows that convention rather than adding ``twilio-python``.
"""

from __future__ import annotations

from urllib.parse import quote, urlencode
from xml.sax.saxutils import escape

import httpx

from koel.common.http import make_async_client
from koel.common.logging import get_logger
from koel.db.models import CallOutcome
from koel.voicebot.campaigns import record_dial_result

logger = get_logger("koel.voicebot.outbound_dialer")

_TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"
_CALL_CREATE_TIMEOUT_S = 10.0

# Orchestrator CallOutcome (rich, mid-call vocabulary) -> dialer retry vocabulary
# (koel/voicebot/pipeline/dialer.py's TERMINAL_OUTCOMES / DEFAULT_RETRY_OUTCOMES).
# Anything not mapped here is intentionally left as-is: the dialer treats an
# unrecognised outcome as non-retryable (fail closed), which is the right
# default for an outcome this table doesn't have a considered mapping for yet.
_OUTCOME_TO_DIAL_OUTCOME: dict[str, str] = {
    CallOutcome.converted.value: "completed",
    CallOutcome.declined.value: "completed",
    CallOutcome.escalated.value: "completed",
    CallOutcome.no_answer.value: "no_answer",
    CallOutcome.customer_unresponsive.value: "no_answer",
    CallOutcome.call_dropped.value: "network_error",
    CallOutcome.unsupported_language.value: "completed",
    CallOutcome.in_progress.value: "network_error",
}

# Twilio call-status webhook values (StatusCallbackEvent) that mean the leg
# never reached a live human on this attempt — reported before the Stream ever
# connects, so these never reach the orchestrator's own CallOutcome vocabulary.
_STATUS_TO_DIAL_OUTCOME: dict[str, str] = {
    "no-answer": "no_answer",
    "busy": "busy",
    "failed": "failed",
    "canceled": "failed",
}


def call_outcome_to_dial_outcome(call_outcome: str) -> str:
    """Translate a finished call's ``CallOutcome`` into the dialer's retry
    vocabulary. See the module-level table for the mapping and its rationale."""
    return _OUTCOME_TO_DIAL_OUTCOME.get(call_outcome, call_outcome)


def call_status_to_dial_outcome(call_status: str) -> str | None:
    """Translate a Twilio ``CallStatus`` webhook value into the dialer's retry
    vocabulary. Returns ``None`` for statuses that aren't a final outcome
    (queued/ringing/in-progress/completed — "completed" here just means the
    Stream connected and the orchestrator's own outcome is authoritative)."""
    return _STATUS_TO_DIAL_OUTCOME.get(call_status)


def _stream_twiml(*, stream_url: str) -> str:
    """Inline TwiML connecting the call straight to the Media Streams bridge —
    no separate TwiML-serving webhook needed. ``stream_url`` is XML-escaped
    (it carries the attempt/bot ids as query params) even though those are
    server-generated ids, not raw user input, as defense in depth."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Connect><Stream url=\"{escape(stream_url)}\"/></Connect></Response>"
    )


def _to_wss(public_base_url: str) -> str:
    """``https://host`` (or ``http://``) -> ``wss://host`` — Twilio Media
    Streams always connects over a websocket regardless of the site's own
    scheme."""
    if public_base_url.startswith("https://"):
        return "wss://" + public_base_url[len("https://") :]
    if public_base_url.startswith("http://"):
        return "wss://" + public_base_url[len("http://") :]
    return public_base_url


class OutboundDialerDisabled(Exception):
    """Raised (and caught internally) when origination is attempted without
    the carrier being configured — mirrors the fail-closed dormancy gate every
    other Twilio integration point in this codebase uses."""


async def originate_call(
    attempt: dict,
    *,
    bot_id: str,
    account_sid: str,
    auth_token: str,
    from_number: str,
    public_base_url: str,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    """Place one outbound PSTN call for a claimed :class:`DialAttempt`, pointed
    at ``/twilio/stream`` (the existing inbound bridge route, now also carrying
    ``attempt_id``/``bot_id`` query params so it can resolve an OUTBOUND leg).

    Returns the Twilio Call SID on success. On any origination-level failure
    (missing config, network error, non-2xx from Twilio) records the attempt
    as ``network_error`` (retryable — this is a placement failure, not a
    customer-side signal) and returns ``None``. Never raises to the caller —
    a bad attempt must not crash the whole claim batch.
    """
    if not (account_sid and auth_token and from_number and public_base_url):
        logger.debug(
            "outbound_dial_skipped_unconfigured",
            extra={"extra_fields": {"attempt_id": attempt["id"]}},
        )
        return None

    stream_url = (
        f"{_to_wss(public_base_url)}/twilio/stream"
        f"?attempt_id={quote(attempt['id'])}&bot_id={quote(bot_id)}"
    )
    twiml = _stream_twiml(stream_url=stream_url)
    status_callback = (
        f"{public_base_url.rstrip('/')}/twilio/call-status?attempt_id={quote(attempt['id'])}"
    )

    # httpx's `data=` only form-encodes a Mapping — a list of tuples (needed
    # here for the repeated StatusCallbackEvent field) silently falls through
    # to a raw-content path instead, which breaks on an AsyncClient. Encode by
    # hand instead; urlencode natively supports repeated keys from a list of
    # pairs (no `doseq` needed — that's for a dict-of-sequences).
    body = urlencode(
        [
            ("To", attempt["phone"]),
            ("From", from_number),
            ("Twiml", twiml),
            ("StatusCallback", status_callback),
            ("StatusCallbackEvent", "completed"),
            ("StatusCallbackEvent", "no-answer"),
            ("StatusCallbackEvent", "busy"),
            ("StatusCallbackEvent", "failed"),
        ]
    )

    owns_client = client is None
    client = client or make_async_client(timeout_s=_CALL_CREATE_TIMEOUT_S, http2=False)
    try:
        resp = await client.post(
            f"{_TWILIO_API_BASE}/Accounts/{account_sid}/Calls.json",
            auth=(account_sid, auth_token),
            content=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        call_sid = resp.json().get("sid")
        logger.info(
            "outbound_call_originated",
            extra={"extra_fields": {"attempt_id": attempt["id"], "call_sid": call_sid}},
        )
        return call_sid
    except Exception:  # noqa: BLE001 — a placement failure must not crash the batch
        logger.warning(
            "outbound_call_origination_failed",
            extra={"extra_fields": {"attempt_id": attempt["id"]}},
            exc_info=True,
        )
        await record_dial_result(attempt["id"], "network_error")
        return None
    finally:
        if owns_client:
            await client.aclose()
