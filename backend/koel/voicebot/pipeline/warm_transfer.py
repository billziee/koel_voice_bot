"""PSTN warm transfer (3.1 Phase B) — the carrier-level handoff that Phase A's
browser operator console (text-relay through the bot's own TTS) doesn't cover.

Rather than Twilio TaskRouter (a routing/queueing product needing a
provisioned Workspace/Workflow/TaskQueue this project has no live account to
set up or verify against), this uses Twilio's Conference API directly: the
same real-world outcome — the operator ends up on the phone with the
customer — via a smaller, well-documented, stable REST surface. Two calls
share one ad-hoc conference room:

  1. The customer's live call (already in progress) is REST-redirected into
     ``<Dial><Conference>``.
  2. A new outbound call rings the operator with a short whisper (context:
     who's calling, why) before joining the SAME conference.

Dormant like every other Twilio integration in this codebase: needs
``twilio_account_sid``/``auth_token``/``from_number`` configured. Requires a
PSTN call in the first place (``SessionState.twilio_call_sid`` is None for a
browser call) and an operator phone number, which isn't a stored field on the
User model — passed explicitly by the caller (the operator-facing API) so
this module makes no assumption about where that number lives.
"""

from __future__ import annotations

from urllib.parse import urlencode
from xml.sax.saxutils import escape

import httpx

from koel.common.http import make_async_client
from koel.common.logging import get_logger

logger = get_logger("koel.voicebot.warm_transfer")

_TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"
_REQUEST_TIMEOUT_S = 10.0


def _conference_name(call_sid: str) -> str:
    return f"koel-escalation-{call_sid}"


def _redirect_to_conference_twiml(conference_name: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Dial><Conference>{escape(conference_name)}</Conference></Dial></Response>"
    )


def _whisper_then_join_twiml(conference_name: str, whisper_text: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Say>{escape(whisper_text)}</Say>"
        f"<Dial><Conference>{escape(conference_name)}</Conference></Dial></Response>"
    )


async def end_twilio_call(
    *,
    call_sid: str,
    account_sid: str,
    auth_token: str,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Terminate a live PSTN leg via Twilio's REST Calls API (``Status=completed``).

    Media Streams gives an app no way to hang up a call from its own side —
    the `stop` event is Twilio→app only, and closing our WebSocket doesn't
    reliably end the underlying call (it depends on the TwiML verb that opened
    the stream). This is the actual mechanism: when the orchestrator decides
    mid-call to end a PSTN call (opt-out, a scripted close, the no-input or
    budget guard), the telephony call session calls this so the customer is
    really disconnected — the browser transport's equivalent is simply closing
    its WebSocket, which telephony can't rely on for the same effect. Returns
    True iff Twilio accepted the request; never raises.
    """
    if not (account_sid and auth_token):
        logger.debug(
            "end_twilio_call_skipped_unconfigured", extra={"extra_fields": {"call_sid": call_sid}}
        )
        return False
    owns_client = client is None
    client = client or make_async_client(timeout_s=_REQUEST_TIMEOUT_S, http2=False)
    try:
        resp = await client.post(
            f"{_TWILIO_API_BASE}/Accounts/{account_sid}/Calls/{call_sid}.json",
            auth=(account_sid, auth_token),
            content=urlencode({"Status": "completed"}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        logger.info("twilio_call_ended_via_rest", extra={"extra_fields": {"call_sid": call_sid}})
        return True
    except Exception:  # noqa: BLE001 — best-effort; the call is already ended state-side
        logger.warning(
            "end_twilio_call_failed", extra={"extra_fields": {"call_sid": call_sid}}, exc_info=True
        )
        return False
    finally:
        if owns_client:
            await client.aclose()


async def start_warm_transfer(
    *,
    call_sid: str,
    operator_phone: str,
    account_sid: str,
    auth_token: str,
    from_number: str,
    whisper_text: str = "Connecting you to an escalated customer call now.",
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Move the live customer call and a new outbound call to the operator
    into the same ad-hoc conference. Returns True iff both legs were placed;
    False (never raises) on any origination-level failure or missing config —
    the caller should keep the existing Phase-A text relay as a fallback."""
    if not (account_sid and auth_token and from_number):
        logger.debug("warm_transfer_skipped_unconfigured", extra={"extra_fields": {"call_sid": call_sid}})
        return False

    conference_name = _conference_name(call_sid)
    owns_client = client is None
    client = client or make_async_client(timeout_s=_REQUEST_TIMEOUT_S, http2=False)
    try:
        redirect_resp = await client.post(
            f"{_TWILIO_API_BASE}/Accounts/{account_sid}/Calls/{call_sid}.json",
            auth=(account_sid, auth_token),
            content=urlencode({"Twiml": _redirect_to_conference_twiml(conference_name)}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        redirect_resp.raise_for_status()

        operator_resp = await client.post(
            f"{_TWILIO_API_BASE}/Accounts/{account_sid}/Calls.json",
            auth=(account_sid, auth_token),
            content=urlencode(
                {
                    "To": operator_phone,
                    "From": from_number,
                    "Twiml": _whisper_then_join_twiml(conference_name, whisper_text),
                }
            ),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        operator_resp.raise_for_status()
        logger.info(
            "warm_transfer_started",
            extra={"extra_fields": {"call_sid": call_sid, "conference": conference_name}},
        )
        return True
    except Exception:  # noqa: BLE001 — a placement failure must not crash the claim route
        logger.warning(
            "warm_transfer_failed",
            extra={"extra_fields": {"call_sid": call_sid}},
            exc_info=True,
        )
        return False
    finally:
        if owns_client:
            await client.aclose()
