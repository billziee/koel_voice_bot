"""Shared post-call side effects (roadmap 2.4 / 5.3) — fired off the critical
path right after a call is safely persisted, so a slow/failed CRM summary or
follow-up dispatch can never delay socket teardown. Used by BOTH transports
(``ws/handler.CallSession`` and the telephony call session) so a phone call's
analytics/follow-up don't depend on which transport handled it.
"""

from __future__ import annotations

import asyncio

from koel.common.logging import get_logger
from koel.voicebot.config import VoiceBotSettings
from koel.voicebot.persistence import persist_call_summary
from koel.voicebot.pipeline.followup import send_followup
from koel.voicebot.pipeline.summarizer import SarvamSummarizer
from koel.voicebot.session.models import SessionState

logger = get_logger("koel.voicebot.postcall")


def dispatch_followup(state: SessionState, background_tasks: set[asyncio.Task]) -> None:
    """Fire the post-call follow-up (2.4) off the critical path. No-op unless a
    template matches the outcome and the WhatsApp channel is configured."""
    if not state.followup_templates:
        return
    task = asyncio.create_task(send_followup(state))
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


def summarize_and_annotate_crm(
    state: SessionState,
    *,
    clients,
    settings: VoiceBotSettings,
    background_tasks: set[asyncio.Task],
) -> None:
    """Best-effort post-call CRM note (5.3): an LLM extracts interest/objection/
    sentiment/next-action from the transcript and appends it to the customer's
    record. Never on the critical path."""
    if not state.customer_id or not settings.sarvam_enabled or not state.transcript:
        return
    task = asyncio.create_task(_run_summary(state, clients=clients, settings=settings))
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


async def _run_summary(state: SessionState, *, clients, settings: VoiceBotSettings) -> None:
    summarizer = SarvamSummarizer(
        api_key=settings.sarvam_api_key,  # type: ignore[arg-type]  # sarvam_enabled guards None
        model=settings.sarvam_summary_model,
        base_url=settings.sarvam_base_url,
        timeout_s=settings.sarvam_summary_timeout_s,
    )
    try:
        summary = await summarizer.summarize(state.transcript)
        # persist_call_summary self-swallows, so a CallLog update failure can't
        # stop the CRM annotation below.
        await persist_call_summary(
            state.session_id,
            {
                "interest_shown": summary.interest_shown,
                "objection": summary.objection,
                "sentiment": summary.sentiment,
                "next_action": summary.next_action,
            },
        )
        await clients.add_call_summary(
            state.customer_id,  # type: ignore[arg-type]  # guarded by caller
            session_id=state.session_id,
            interest_shown=summary.interest_shown,
            objection=summary.objection,
            sentiment=summary.sentiment,
            next_action=summary.next_action,
        )
    except Exception:  # noqa: BLE001 — best-effort CRM annotation, never raise
        logger.warning("call_summary_failed session=%s", state.session_id, exc_info=True)
    finally:
        await summarizer.aclose()
