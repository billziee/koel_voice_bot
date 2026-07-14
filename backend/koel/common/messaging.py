"""Outbound messaging — SMS (2.1) and WhatsApp (2.4).

A single ``send_message(channel, to, body)`` over a generic HTTP gateway. Each
channel is DORMANT until its gateway URL + key are configured: a send is then a
logged no-op returning False, so in-call revenue actions and post-call follow-ups
degrade cleanly with no credentials (per the "leave keys empty" constraint).
Swap the tiny adapter below for a Twilio/MSG91/Meta client when going live.
"""

from __future__ import annotations

from enum import Enum

from koel.common.config import CommonSettings, get_common_settings
from koel.common.http import make_async_client
from koel.common.logging import get_logger

logger = get_logger("koel.messaging")


class Channel(str, Enum):
    sms = "sms"
    whatsapp = "whatsapp"


def _gateway(channel: Channel, settings: CommonSettings) -> tuple[str, str]:
    if channel is Channel.whatsapp:
        return settings.whatsapp_gateway_url, settings.whatsapp_api_key
    return settings.sms_gateway_url, settings.sms_api_key


async def send_message(
    channel: Channel,
    to: str,
    body: str,
    *,
    settings: CommonSettings | None = None,
    idempotency_key: str | None = None,
) -> bool:
    """Send a message; return True on accepted delivery, False when the channel
    is dormant (unconfigured) or the send fails. Best-effort — never raises, so a
    messaging outage can't break a call or its teardown."""
    settings = settings or get_common_settings()
    url, key = _gateway(channel, settings)
    if not (url and key and to):
        logger.info(
            "message_skipped (channel dormant / no recipient) channel=%s configured=%s",
            channel.value, bool(url and key),
        )
        return False
    payload = {"to": to, "body": body, "channel": channel.value}
    headers = {"Authorization": f"Bearer {key}"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    try:
        async with make_async_client(timeout_s=settings.messaging_timeout_s) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
        logger.info("message_sent channel=%s", channel.value)
        return True
    except Exception:  # noqa: BLE001 — never fail the caller on a messaging error
        logger.warning("message_send_failed channel=%s", channel.value, exc_info=True)
        return False
