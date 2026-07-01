"""The turn orchestrator (PRD §05 Data Flow + Conversation Resilience).

Given a final customer transcript, it:
  1. checks language confidence → graceful fallback + escalation if unsupported
  2. races NLP generation against the filler trigger → injects filler on a slow turn
  3. applies escalation / opt-out / end-call rules
  4. synthesizes the response (tone → prosody) and streams audio back
  5. records every turn on the session state

It is transport-agnostic: it takes a ``send`` callback so the same logic serves
the WebSocket handler today and a telephony bridge later.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from koel.common.logging import get_logger
from koel.common.schemas import Language, Tone
from koel.common.telemetry import current_trace_id, span
from koel.common.text_guard import redact_pii
from koel.nlp.schemas import Intent, NlpResponse, TurnContext
from koel.stt.schemas import TranscriptResult
from koel.tts.schemas import SynthesizeRequest
from koel.voicebot.clients import ServiceClients
from koel.voicebot.config import VoiceBotSettings
from koel.voicebot.pipeline.filler import FillerCache, filler_tone_for
from koel.voicebot.session.models import CallStatus, SessionState, Turn
from koel.voicebot.ws.protocol import ServerMessage, ServerMessageType

logger = get_logger("koel.voicebot.orchestrator")

Sender = Callable[[ServerMessage], Awaitable[None]]

# Graceful unsupported-language message (PRD §06 Graceful Language Fallback).
_FALLBACK_TEXT = (
    "Maaf kijiye, main keval Hindi ya English samajh sakti hoon. "
    "Main aapko ek agent se connect karti hoon."
)


class Orchestrator:
    def __init__(
        self,
        *,
        settings: VoiceBotSettings,
        clients: ServiceClients,
        filler_cache: FillerCache,
    ) -> None:
        self._settings = settings
        self._clients = clients
        self._filler = filler_cache

    async def _send_bot_turn(
        self,
        state: SessionState,
        send: Sender,
        *,
        text: str,
        tone: Tone,
        intent: str | None = None,
        ambient: bool = True,
    ) -> Turn:
        """Synthesize + stream a bot utterance and append it to the transcript.

        ``ambient`` mixes the call-floor bed under the speech (PRD §06); callers
        set it False to mute the bed during an escalation handoff.
        """
        # Never speak/persist a raw account/card/phone number (PRD §11/§14). Names
        # and short figures (fees, rewards) are preserved — see redact_pii.
        text = redact_pii(text)
        turn = Turn(
            index=state.next_turn_index(),
            speaker="bot",
            text=text,
            language=state.language,
            tone=tone,
            intent=intent,
        )
        state.add_turn(turn)
        state.last_confirmed_turn_index = turn.index

        await send(ServerMessage.of(ServerMessageType.call_state, status=CallStatus.bot_speaking))
        await send(
            ServerMessage.of(
                ServerMessageType.transcript,
                speaker="bot",
                text=text,
                tone=tone.value,
                index=turn.index,
            )
        )
        with span("tts.synthesize", session_id=state.session_id, tone=tone.value):
            # Capture the trace id for the §11 audit / §14 latency dashboards.
            if state.trace_id is None:
                state.trace_id = current_trace_id()
            audio = await self._clients.synthesize(
                SynthesizeRequest(
                    session_id=state.session_id,
                    text=text,
                    language=state.language,
                    tone=tone,
                    speaking_rate=1.0,
                    ambient=ambient,
                    ambient_profile=state.ambient_profile if ambient else None,
                )
            )
        await send(
            ServerMessage.of(
                ServerMessageType.audio_chunk,
                audio_b64=audio.audio_b64,
                duration_ms=audio.duration_ms,
                index=turn.index,
                sample_rate=audio.sample_rate,
                audio_format=audio.audio_format,
            )
        )
        return turn

    async def greet(self, state: SessionState, send: Sender) -> None:
        """Opening turn (PRD §05 step 2).

        The bot MUST self-identify as automated (PRD §12 trust / fair-practice)
        and capture consent at call open (PRD §11 DPDP/IRDAI). We render the
        bot's configured greeting template with CRM fields, then guarantee the
        automated-call disclosure is present regardless of the template.
        """
        name = state.crm_context.get("name", "")
        bank = state.crm_context.get("bank", "the bank")

        if state.greeting_template:
            greeting = self._render_template(state.greeting_template, state.crm_context)
        else:
            greeting = f"Namaste {name} ji, main {bank} ki taraf se KOEL bol rahi hoon."

        disclosure = "Yeh ek automated call hai."
        if disclosure.lower() not in greeting.lower() and "automated" not in greeting.lower():
            greeting = f"{greeting} {disclosure}"
        greeting = f"{greeting} Do minute baat kar sakte hain?"

        await self._send_bot_turn(state, send, text=greeting, tone=state.default_tone, intent="greeting")
        # Consent captured at call open with the automated disclosure spoken,
        # stamped with a timestamp for the §11 audit trail.
        state.consent_captured = True
        state.consent_at = datetime.now(UTC)
        state.status = CallStatus.listening
        await send(ServerMessage.of(ServerMessageType.call_state, status=CallStatus.listening))

    @staticmethod
    def _render_template(template: str, crm: dict) -> str:
        """Safe placeholder fill: replaces {key} / {a.b} tokens from CRM, leaves unknowns."""
        out = template
        for key, value in crm.items():
            out = out.replace(f"{{{key}}}", str(value))
            out = out.replace(f"{{customer.{key}}}", str(value))
            out = out.replace(f"{{offer.{key}}}", str(value))
        out = out.replace("{bank}", str(crm.get("bank", "the bank")))
        return out

    async def handle_final_transcript(
        self, state: SessionState, send: Sender, stt: TranscriptResult
    ) -> None:
        """Process one completed customer utterance through the pipeline."""
        # Reset no-input bookkeeping — the customer spoke.
        state.no_input_strikes = 0

        # Record the customer turn.
        cust_turn = Turn(
            index=state.next_turn_index(),
            speaker="customer",
            text=stt.text,
            language=stt.language_id or state.language,
            confidence=stt.word_confidence,
        )
        state.add_turn(cust_turn)
        await send(
            ServerMessage.of(
                ServerMessageType.transcript,
                speaker="customer",
                text=stt.text,
                index=cust_turn.index,
            )
        )

        # --- Graceful language fallback (PRD §06) ---
        if self._is_unsupported_language(stt):
            await self._escalate_unsupported_language(state, send)
            return

        # Mid-call language switch (PRD §06), but only to a supported language.
        if stt.language_id and stt.language_id in (Language.en, Language.hi, Language.hinglish):
            state.language = stt.language_id

        await send(ServerMessage.of(ServerMessageType.call_state, status=CallStatus.processing))
        state.status = CallStatus.processing

        # --- NLP with filler racing (PRD §05 Latency Masking) ---
        nlp = await self._respond_with_filler(state, send)

        # --- Apply intent-driven control flow ---
        if nlp.intent == Intent.opt_out:
            # TRAI (PRD §11): honour the opt-out immediately — flag the CRM,
            # speak one acknowledgement, and END the call now. Do not continue
            # the script or wait on a separate end_call signal.
            await self._clients.flag_customer(state.customer_id or "", "opt_out", "customer opt-out")
            await self._send_bot_turn(
                state, send, text=nlp.response_text, tone=nlp.tone, intent=nlp.intent.value
            )
            await self.end_call(state, send, outcome="declined")
            return
        if nlp.intent == Intent.busy:
            # Deferral → schedule a callback rather than treat it as a loss.
            await self._clients.flag_customer(state.customer_id or "", "callback", "customer busy")
        await self._send_bot_turn(
            state, send, text=nlp.response_text, tone=nlp.tone, intent=nlp.intent.value
        )

        if nlp.escalate:
            await self._escalate(state, send, reason=nlp.escalation_reason or "intent")
            return
        if nlp.end_call:
            # Only a genuinely positive close converts; deferrals/declines do not.
            positive = nlp.intent in (Intent.interested, Intent.high_interest, Intent.promise_to_pay)
            outcome = "converted" if positive else "declined"
            await self.end_call(state, send, outcome=outcome)
            return

        state.status = CallStatus.listening
        await send(ServerMessage.of(ServerMessageType.call_state, status=CallStatus.listening))

    async def _respond_with_filler(self, state: SessionState, send: Sender) -> NlpResponse:
        """Run NLP; if it exceeds the filler trigger, inject a filler clip meanwhile."""
        ctx = TurnContext(
            session_id=state.session_id,
            bot_id=state.bot_id,
            use_case=state.use_case,
            language=state.language,
            customer_text=state.transcript[-1].text,
            history=state.recent_history(),
            crm_context=state.crm_context,
            rag_enabled=state.rag_enabled,
            rag_index=state.rag_index,
            rag_top_k=state.rag_top_k,
            rag_score_threshold=state.rag_score_threshold,
        )
        with span("nlp.respond", session_id=state.session_id):
            nlp_task = asyncio.create_task(self._clients.respond(ctx))
            trigger_s = self._settings.filler_trigger_ms / 1000
            try:
                return await asyncio.wait_for(asyncio.shield(nlp_task), timeout=trigger_s)
            except TimeoutError:
                await self._inject_filler(state, send)
                return await nlp_task

    async def _inject_filler(self, state: SessionState, send: Sender) -> None:
        clip = self._filler.next_clip(state.session_id, state.language)
        if clip is None:
            return  # cache not warmed; degrade silently (no dead-air guarantee lost elsewhere)
        text, audio_b64 = clip
        logger.info("filler_injected", extra={"extra_fields": {"session_id": state.session_id}})
        await send(
            ServerMessage.of(
                ServerMessageType.filler,
                text=text,
                audio_b64=audio_b64,
                tone=filler_tone_for(state.use_case).value,
            )
        )

    def _is_unsupported_language(self, stt: TranscriptResult) -> bool:
        supported = {Language.en, Language.hi, Language.hinglish}
        if stt.language_id is None:
            return False
        return (
            stt.language_id not in supported
            and stt.language_confidence >= self._settings.language_confidence_threshold
        )

    async def _escalate_unsupported_language(self, state: SessionState, send: Sender) -> None:
        # Mute the ambient bed for the handoff line (PRD §06).
        await self._send_bot_turn(
            state, send, text=_FALLBACK_TEXT, tone=Tone.empathetic, ambient=False
        )
        if state.customer_id:
            await self._clients.flag_customer(
                state.customer_id, "unsupported_language", "language outside EN/HI/Hinglish"
            )
        state.outcome = "unsupported_language"
        await self._escalate(state, send, reason="unsupported_language")

    async def _escalate(self, state: SessionState, send: Sender, *, reason: str) -> None:
        state.escalated = True
        state.status = CallStatus.escalating
        await send(
            ServerMessage.of(
                ServerMessageType.call_state,
                status=CallStatus.escalating,
                reason=reason,
                # Full context handoff (PRD §02 Escalation Quality = 100%).
                transcript=[t.model_dump(mode="json") for t in state.transcript],
                crm_context=state.crm_context,
            )
        )
        await self.end_call(state, send, outcome="escalated")

    async def end_call(self, state: SessionState, send: Sender, *, outcome: str) -> None:
        if state.status == CallStatus.ended:
            return
        state.status = CallStatus.ended
        if state.outcome == "in_progress":
            state.outcome = outcome
        await send(
            ServerMessage.of(
                ServerMessageType.call_ended,
                outcome=state.outcome,
                escalated=state.escalated,
                turns=len(state.transcript),
            )
        )
