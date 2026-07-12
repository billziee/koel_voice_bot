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
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from koel.common.logging import get_logger
from koel.common.schemas import Language, Tone
from koel.common.telemetry import (
    current_trace_id,
    record_hop_latency,
    record_turn_latency,
    span,
)
from koel.common.text_guard import redact_pii
from koel.nlp.schemas import Intent, NlpResponse, Sentiment, TurnContext
from koel.stt.schemas import TranscriptResult
from koel.tts.schemas import SynthesizeRequest
from koel.voicebot.clients import ServiceClients
from koel.voicebot.config import VoiceBotSettings
from koel.voicebot.pipeline import localization
from koel.voicebot.pipeline.cost import estimate_turn_cost_usd
from koel.voicebot.pipeline.filler import FillerCache, filler_tone_for
from koel.voicebot.session.models import CallStatus, SessionState, Turn
from koel.voicebot.ws.protocol import ServerMessage, ServerMessageType

logger = get_logger("koel.voicebot.orchestrator")

Sender = Callable[[ServerMessage], Awaitable[None]]

# Explicit "switch language" request in the caller's words (reliable, unlike STT
# language guessing). A request cue + a language word → lock that language.
_LANG_REQUEST_CUE = re.compile(
    r"(continue|speak|switch|talk|reply|answer|prefer|in\b|karo|karein|bol|baat|mein|me\b)", re.I
)
_LANG_WORDS: list[tuple[Language, re.Pattern]] = [
    (Language.en, re.compile(r"\b(english|angre[zj]i)\b", re.I)),
    (Language.hi, re.compile(r"(\bhindi\b|हिंदी|हिन्दी)", re.I)),
]


def _requested_language(text: str) -> Language | None:
    """Detect an explicit caller request to switch language (e.g. 'continue in
    English', 'Hindi mein baat karo'). Returns None if it isn't clearly a request."""
    if not text or not _LANG_REQUEST_CUE.search(text):
        return None
    for lang, pat in _LANG_WORDS:
        if pat.search(text):
            return lang
    return None


# In-call tone adaptation (roadmap 4.3). Number of trailing customer turns that
# must ALL read negative before the bot softens to an empathetic tone — two keeps
# it responsive without overreacting to a single terse reply. The rolling window
# on SessionState is capped a little wider for observability.
_NEGATIVE_TONE_WINDOW = 2
_SENTIMENT_HISTORY_MAX = 5


def sentiment_adjusted_tone(recent_sentiments: list[str], llm_tone: Tone) -> Tone:
    """Soften the bot's tone when the customer's recent sentiment trends negative.

    If the last ``_NEGATIVE_TONE_WINDOW`` customer turns all read negative, force an
    empathetic tone regardless of what NLP proposed for this turn; otherwise keep
    the NLP-selected tone. Pure and deterministic, so it unit-tests in isolation.
    """
    window = recent_sentiments[-_NEGATIVE_TONE_WINDOW:]
    if len(window) >= _NEGATIVE_TONE_WINDOW and all(
        s == Sentiment.negative.value for s in window
    ):
        return Tone.empathetic
    return llm_tone


# Graceful unsupported-language message (PRD §06 Graceful Language Fallback).
_FALLBACK_TEXT = (
    "Maaf kijiye, main keval Hindi ya English samajh sakti hoon. "
    "Main aapko ek agent se connect karti hoon."
)

# Greeting must be in the BOT'S language — an English bot spoke Hinglish because the
# default greeting + the consent suffix were hardcoded Hinglish. Keyed by Language;
# Hindi is Devanagari (spoken by Magpie), Hinglish stays romanized. `{name}`/`{bank}`
# are filled from CRM. Falls back to Hinglish for any unmapped language.
# Copy must match each language's TTS: en/hinglish → Orpheus (English/romanized
# Latin), hi → Magpie/Sarvam (Devanagari only — romanized Hindi comes out as
# gibberish). English brand/banking terms stay Latin. `{name}`/`{bank}` fill
# from CRM — the placeholder tokens survive transliteration intact (verified:
# Sarvam only converts the Devanagari script, ASCII braces pass through).
# hinglish is derived from hi via Sarvam transliteration at startup, not
# hand-authored — see koel/voicebot/pipeline/localization.py.
_DEFAULT_GREETING = localization.register(localization.Localized(
    en="Hello {name}, this is KOEL calling on behalf of {bank}.",
    hi="नमस्ते {name} जी, मैं {bank} की ओर से KOEL बोल रही हूँ।",
    hinglish_fallback="Namaste {name} ji, main {bank} ki taraf se KOEL bol rahi hoon.",
))
_DISCLOSURE = localization.register(localization.Localized(
    en="This is an automated call.",
    hi="यह एक स्वचालित कॉल है।",
    hinglish_fallback="Yeh ek automated call hai.",
))
_CONSENT_ASK = localization.register(localization.Localized(
    en="Do you have two minutes to talk?",
    hi="क्या आप दो मिनट बात कर सकते हैं?",
    hinglish_fallback="Do minute baat kar sakte hain?",
))
# Polite close when a per-session cost budget is hit (O-9). Never mentions the
# budget — a natural "I'll follow up" close, script-safe in each language.
_BUDGET_WRAPUP = localization.register(localization.Localized(
    en="I don't want to take more of your time — I'll follow up with the details shortly. Thank you!",
    hi="मैं आपका और समय नहीं लेना चाहती — मैं जल्द ही आपको पूरी जानकारी भेज दूँगी। धन्यवाद!",
    hinglish_fallback=(
        "Main aapka aur time nahi lena chahti — main jaldi hi aapko poori detail "
        "bhej deti hoon. Dhanyavaad!"
    ),
))

# Hold line spoken as the call is handed to a human agent (3.1).
_HOLD_FOR_AGENT = localization.register(localization.Localized(
    en="Let me connect you with a specialist who can help — please stay on the line a moment.",
    hi="मैं आपको एक विशेषज्ञ से जोड़ रही हूँ जो आपकी मदद करेंगे — कृपया एक पल लाइन पर बने रहें।",
    hinglish_fallback=(
        "Main aapko ek specialist se connect kar rahi hoon jo aapki help karenge — "
        "kripya ek pal line par baney rahiye."
    ),
))


def _by_language(table: localization.Localized, language: Language) -> str:
    """Language-appropriate copy, defaulting to Hinglish (the demo's lingua franca)."""
    return table.get(language)


# ambient_profile values that mean "no floor chatter" — set by the FE on/off toggle.
_AMBIENT_OFF = {"", "silent", "none", "off"}


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

        # Ambient bed is per-bot: an "off"/"silent" profile disables the floor chatter
        # (the Bot Builder / Config toggle sets the profile). It's mixed INTO the clip,
        # so it only ever plays while the bot speaks — never as a standalone loop.
        use_ambient = ambient and state.ambient_profile.strip().lower() not in _AMBIENT_OFF

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
        req = SynthesizeRequest(
            session_id=state.session_id,
            text=text,
            language=state.language,
            tone=tone,
            voice_gender=state.voice_gender,
            speaking_rate=state.speaking_rate,
            ambient=use_ambient,
            ambient_profile=state.ambient_profile if use_ambient else None,
            telephony=self._settings.telephony_mode,
        )
        with span("tts.synthesize", session_id=state.session_id, tone=tone.value) as tts_span:
            # Capture the trace id for the §11 audit / §14 latency dashboards.
            if state.trace_id is None:
                state.trace_id = current_trace_id()
            t0 = time.monotonic()
            if self._settings.stream_tts:
                # Sentence-streaming (Tier 2): play the first sentence as soon as
                # it's synthesized instead of waiting for the whole clip. Each chunk
                # is a complete WAV the browser decodes + plays gaplessly.
                first_ms: float | None = None
                async for chunk in self._clients.stream_synthesis(req):
                    if first_ms is None:
                        first_ms = (time.monotonic() - t0) * 1000
                        record_hop_latency("tts_first_audio", first_ms)
                    await send(
                        ServerMessage.of(
                            ServerMessageType.audio_chunk,
                            audio_b64=chunk.audio_b64,
                            duration_ms=chunk.duration_ms,
                            index=turn.index,
                            seq=chunk.seq,
                            sample_rate=chunk.sample_rate,
                            audio_format=chunk.audio_format,
                        )
                    )
                tts_span.set_attribute("first_audio_ms", round(first_ms or 0.0, 1))
            else:
                audio = await self._clients.synthesize(req)
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
                tts_span.set_attribute("fallback_used", audio.fallback_used)
            tts_ms = (time.monotonic() - t0) * 1000
            record_hop_latency("tts", tts_ms)
            tts_span.set_attribute("synthesize_latency_ms", round(tts_ms, 1))
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
        lang = state.language

        if state.greeting_template:
            greeting = self._render_template(state.greeting_template, state.crm_context)
        else:
            greeting = _by_language(_DEFAULT_GREETING, lang).format(name=name, bank=bank)

        # Automated-call disclosure + consent ask, spoken in the bot's language so an
        # en/hi bot never gets Hinglish tacked on (§12 trust / §11 consent).
        disclosure = _by_language(_DISCLOSURE, lang)
        if disclosure.lower() not in greeting.lower() and "automated" not in greeting.lower():
            greeting = f"{greeting} {disclosure}"
        greeting = f"{greeting} {_by_language(_CONSENT_ASK, lang)}"

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
        turn_start = time.monotonic()
        try:
            await self._handle_final_transcript(state, send, stt)
        finally:
            # End-to-end audio-in→audio-out turn latency vs the §14 <300ms budget (Q).
            record_turn_latency((time.monotonic() - turn_start) * 1000)

    async def _handle_final_transcript(
        self, state: SessionState, send: Sender, stt: TranscriptResult
    ) -> None:
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
        # Off by default: STT language detection on code-mixed audio falsely flags
        # supported speech as "foreign" and ends the call after one turn.
        if self._settings.escalate_on_unsupported_language and self._is_unsupported_language(stt):
            await self._escalate_unsupported_language(state, send)
            return

        # Language control. An EXPLICIT caller request ("continue in English") is a
        # reliable signal — honour it and make it stick for the rest of the call.
        # Otherwise the bot's configured language stays authoritative; STT-detected
        # language only switches when adapt_language_to_caller is on (it's an
        # unreliable guess on code-mixed audio, off by default).
        requested = _requested_language(stt.text)
        if requested is not None:
            state.language = requested
        elif (
            self._settings.adapt_language_to_caller
            and stt.language_id
            and stt.language_id in (Language.en, Language.hi, Language.hinglish)
        ):
            state.language = stt.language_id

        await send(ServerMessage.of(ServerMessageType.call_state, status=CallStatus.processing))
        state.status = CallStatus.processing

        # --- NLP → bot turn ---
        # O-2: with NLP first-token streaming on (and sentence-streaming TTS), stream
        # reply sentences straight into TTS as they generate — the turn is spoken
        # DURING generation, so first-audio is token-bound not completion-bound.
        # Otherwise NLP runs to completion (filler racing the first token) and the
        # turn is spoken by the _send_bot_turn calls below. Batch is the fallback.
        stream_turn = self._settings.stream_nlp and self._settings.stream_tts
        if stream_turn:
            nlp = await self._stream_bot_turn(state, send, self._build_turn_context(state))
        else:
            nlp = await self._respond_with_filler(state, send)

        # In-call tone adaptation (roadmap 4.3): fold this turn's customer sentiment
        # into a rolling window and soften the bot's tone when the trend is negative.
        state.recent_sentiments.append(nlp.customer_sentiment.value)
        state.recent_sentiments = state.recent_sentiments[-_SENTIMENT_HISTORY_MAX:]
        tone = sentiment_adjusted_tone(state.recent_sentiments, nlp.tone)

        # Per-call cost signal (roadmap O-9): every turn adds its estimated
        # STT+NLP+TTS spend to the session running total (enforced below).
        state.running_cost_usd += estimate_turn_cost_usd(
            customer_text=stt.text, bot_text=nlp.response_text
        )

        # --- Apply intent-driven control flow ---
        # When the turn was streamed (O-2), response_text is already spoken — apply
        # the same control flow WITHOUT re-speaking it.
        if nlp.intent == Intent.opt_out:
            # TRAI (PRD §11): honour the opt-out immediately — flag the CRM,
            # speak one acknowledgement, and END the call now. Do not continue
            # the script or wait on a separate end_call signal.
            await self._flag_best_effort(state.customer_id, "opt_out", "customer opt-out")
            if not stream_turn:
                await self._send_bot_turn(
                    state, send, text=nlp.response_text, tone=tone, intent=nlp.intent.value
                )
            await self.end_call(state, send, outcome="declined")
            return
        if nlp.intent == Intent.busy:
            # Deferral → schedule a callback rather than treat it as a loss.
            await self._flag_best_effort(state.customer_id, "callback", "customer busy")
        if not stream_turn:
            await self._send_bot_turn(
                state, send, text=nlp.response_text, tone=tone, intent=nlp.intent.value
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

        # Per-session budget guard (roadmap O-9): if estimated spend has reached
        # the configured ceiling, wrap up politely rather than continue an
        # unbounded (and unboundedly-costed) conversation. Off when budget <= 0.
        budget = self._settings.session_budget_usd
        if budget > 0 and state.running_cost_usd >= budget:
            await self._send_bot_turn(
                state, send,
                text=_by_language(_BUDGET_WRAPUP, state.language),
                tone=Tone.empathetic, intent="budget_reached",
            )
            await self.end_call(state, send, outcome="declined")
            return

        state.status = CallStatus.listening
        await send(ServerMessage.of(ServerMessageType.call_state, status=CallStatus.listening))

    async def _flag_best_effort(self, customer_id: str | None, flag: str, reason: str) -> None:
        """Flag the CRM without letting a CRM outage abort the turn (PRD §12).

        Critical for opt_out: the caller must still hear the acknowledgement and
        have the call end even if the CRM write fails — we do not re-contact them.
        """
        if not customer_id:
            return
        try:
            await self._clients.flag_customer(customer_id, flag, reason)
        except Exception:  # noqa: BLE001 — best-effort; the turn proceeds regardless
            logger.warning("crm_flag_failed flag=%s customer=%s", flag, customer_id, exc_info=True)

    def _build_turn_context(self, state: SessionState) -> TurnContext:
        """The NLP TurnContext for this turn (shared by the batch + streaming paths)."""
        return TurnContext(
            session_id=state.session_id,
            bot_id=state.bot_id,
            org_id=state.org_id,
            use_case=state.use_case,
            language=state.language,
            customer_text=state.transcript[-1].text,
            history=state.recent_history(),
            crm_context=state.crm_context,
            rag_enabled=state.rag_enabled,
            rag_index=state.rag_index,
            rag_top_k=state.rag_top_k,
            rag_score_threshold=state.rag_score_threshold,
            llm_model=state.llm_model,
            llm_temperature=state.llm_temperature,
            prompt_preamble=state.prompt_preamble,
        )

    async def _respond_with_filler(self, state: SessionState, send: Sender) -> NlpResponse:
        """Run NLP; if it exceeds the filler trigger, inject a filler clip meanwhile."""
        ctx = self._build_turn_context(state)
        with span("nlp.respond", session_id=state.session_id):
            t0 = time.monotonic()
            nlp_task = asyncio.create_task(self._clients.respond(ctx))
            trigger_s = self._settings.filler_trigger_ms / 1000
            try:
                result = await asyncio.wait_for(asyncio.shield(nlp_task), timeout=trigger_s)
            except TimeoutError:
                await self._inject_filler(state, send)
                result = await nlp_task
            record_hop_latency("nlp", (time.monotonic() - t0) * 1000)
            return result

    async def _stream_bot_turn(
        self, state: SessionState, send: Sender, ctx: TurnContext
    ) -> NlpResponse:
        """Speak the bot turn WHILE NLP generates it (O-2 first-token streaming).

        Consumes ``clients.respond_stream(ctx)``: each reply sentence is synthesized
        and streamed to the client the moment it lands, so first-audio is token-bound
        rather than whole-completion-bound. The terminal frame carries the full
        ``NlpResponse`` for the caller's control flow. One bot turn, one transcript
        entry. Sentence-streaming TTS *prosody* uses the sentiment-adjusted tone from
        the trend SO FAR (this turn's sentiment isn't known until the reply completes),
        while the persisted ``Turn``/transcript carry the resolved ``final.tone``/intent
        so history matches batch mode. A filler clip masks the wait if the first
        sentence is slower than the filler trigger (same guard the batch path gets).

        Fail-safe (never drops or silently mishandles a live turn):
        - stream error before any audio → batch ``respond()`` speaks the turn;
        - empty/degenerate stream (no sentences) → batch ``respond()`` (loud, not a
          silent bot_speaking→listening flip);
        - stream error AFTER partial audio (lost terminal frame) → reconcile the
          structured fields via batch ``respond()`` so a dropped opt-out/escalate
          isn't ignored; if that also fails, escalate to a human;
        - cancellation (barge-in propagates; server shutdown) → the partial turn is
          still recorded for the audit trail.
        """
        idx = state.next_turn_index()
        # Streamed prosody can't use THIS turn's sentiment (not known until the reply
        # completes), but it can honour the trend so far: soften to empathetic if the
        # prior window already read negative. The persisted Turn uses the resolved
        # final.tone below.
        tone = sentiment_adjusted_tone(state.recent_sentiments, state.default_tone)
        use_ambient = state.ambient_profile.strip().lower() not in _AMBIENT_OFF
        await send(ServerMessage.of(ServerMessageType.call_state, status=CallStatus.bot_speaking))

        spoken: list[str] = []
        final: NlpResponse | None = None
        seq = 0
        first_ms: float | None = None

        recorded = False
        try:
            with span("nlp.stream_turn", session_id=state.session_id) as turn_span:
                if state.trace_id is None:
                    state.trace_id = current_trace_id()
                t0 = time.monotonic()

                async def _speak(sentence: str) -> None:
                    nonlocal seq, first_ms
                    req = SynthesizeRequest(
                        session_id=state.session_id,
                        text=sentence,
                        language=state.language,
                        tone=tone,
                        voice_gender=state.voice_gender,
                        speaking_rate=state.speaking_rate,
                        ambient=use_ambient,
                        ambient_profile=state.ambient_profile if use_ambient else None,
                        telephony=self._settings.telephony_mode,
                    )
                    async for chunk in self._clients.stream_synthesis(req):
                        if first_ms is None:
                            first_ms = (time.monotonic() - t0) * 1000
                            record_hop_latency("tts_first_audio", first_ms)
                        await send(
                            ServerMessage.of(
                                ServerMessageType.audio_chunk,
                                audio_b64=chunk.audio_b64,
                                duration_ms=chunk.duration_ms,
                                index=idx,
                                seq=seq,
                                sample_rate=chunk.sample_rate,
                                audio_format=chunk.audio_format,
                            )
                        )
                        seq += 1

                async def _delayed_filler() -> None:
                    # Filler race for the streaming path (O-2 + PRD §05 latency mask):
                    # if no bot audio has streamed by the filler trigger, mask the wait
                    # with a filler clip — the same guard _respond_with_filler gives the
                    # batch path, which stream mode otherwise skipped.
                    try:
                        await asyncio.sleep(self._settings.filler_trigger_ms / 1000)
                    except asyncio.CancelledError:
                        return
                    if first_ms is None:
                        await self._inject_filler(state, send)

                filler_task = asyncio.create_task(_delayed_filler())
                try:
                    async for frame in self._clients.respond_stream(ctx):
                        if frame.type == "sentence" and frame.text:
                            sentence = redact_pii(frame.text)
                            if sentence:
                                spoken.append(sentence)
                                await _speak(sentence)
                        elif frame.type == "final":
                            final = frame.response
                except Exception:  # noqa: BLE001 — never drop a live turn (CancelledError/barge-in still propagates)
                    logger.warning(
                        "nlp_stream_failed; degrading to batch", extra={"session_id": state.session_id},
                        exc_info=True,
                    )
                    if not spoken:
                        final = await self._clients.respond(ctx)
                        sentence = redact_pii(final.response_text)
                        if sentence:
                            spoken.append(sentence)
                            await _speak(sentence)
                finally:
                    filler_task.cancel()
                turn_span.set_attribute("first_audio_ms", round(first_ms or 0.0, 1))

            if not spoken:
                # Empty / degenerate stream produced no audio. Speak a real turn via
                # batch — loud like batch mode, never a silent bot_speaking→listening
                # flip. A genuinely broken NLP raises here and is surfaced by the
                # receive loop exactly as in batch mode.
                if final is None or not final.response_text.strip():
                    final = await self._clients.respond(ctx)
                sentence = redact_pii(final.response_text)
                if sentence:
                    spoken.append(sentence)
                    await _speak(sentence)
            elif final is None:
                # Spoke ≥1 sentence but lost the terminal frame mid-transport (reset /
                # read-timeout / truncated body). Recover intent/escalate/end_call so a
                # dropped opt-out is never silently ignored (TRAI §11); if the reconcile
                # also fails, hand off to a human rather than continue on unknown intent.
                try:
                    final = await self._clients.respond(ctx)
                except Exception:  # noqa: BLE001 — reconcile failed; escalate, don't continue
                    logger.warning(
                        "nlp_stream_final_lost; escalating", extra={"session_id": state.session_id}
                    )
                    final = NlpResponse(
                        session_id=state.session_id,
                        response_text=" ".join(spoken),
                        intent=Intent.unknown,
                        tone=state.default_tone,
                        escalate=True,
                        escalation_reason="nlp stream lost before final frame",
                    )

            # Persist the RESOLVED tone/intent from the final frame (not the provisional
            # streaming-prosody tone) so history/analytics match batch mode. The audio
            # prosody already streamed on the default tone.
            turn = Turn(
                index=idx,
                speaker="bot",
                text=" ".join(spoken),
                language=state.language,
                tone=final.tone,
                intent=final.intent.value,
            )
            state.add_turn(turn)
            state.last_confirmed_turn_index = turn.index
            recorded = True
            await send(
                ServerMessage.of(
                    ServerMessageType.transcript,
                    speaker="bot",
                    text=turn.text,
                    tone=final.tone.value,
                    index=turn.index,
                )
            )
            return final
        finally:
            # If cancelled (e.g. server shutdown) after speaking but before recording,
            # still persist the partial bot turn so the §11 audit trail + reconnect
            # anchoring don't lose it.
            if not recorded and spoken:
                state.add_turn(
                    Turn(
                        index=idx,
                        speaker="bot",
                        text=" ".join(spoken),
                        language=state.language,
                        tone=tone,
                        intent="unknown",
                    )
                )
                state.last_confirmed_turn_index = idx

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
        # (a) Auto-detect path: STT explicitly reports a non-supported language with
        #     confidence (used when transcription isn't force-pinned).
        if (
            stt.language_id is not None
            and stt.language_id not in supported
            and stt.language_confidence >= self._settings.language_confidence_threshold
        ):
            return True
        # (b) Force-pinned path: STT is pinned to the bot's language, so an
        #     unsupported language is transcribed into it with very LOW confidence.
        #     A sustained, real utterance that still transcribes poorly ⇒ the caller
        #     isn't speaking a language we support. High-confidence Hindi/English is
        #     well clear of the floor, so supported callers are never mis-flagged.
        return bool(
            stt.text.strip()
            and stt.speech_duration_ms >= self._settings.unsupported_min_speech_ms
            and stt.language_confidence < self._settings.unsupported_confidence_floor
        )

    async def _escalate_unsupported_language(self, state: SessionState, send: Sender) -> None:
        # Mute the ambient bed for the handoff line (PRD §06).
        await self._send_bot_turn(
            state, send, text=_FALLBACK_TEXT, tone=Tone.empathetic, ambient=False
        )
        await self._flag_best_effort(
            state.customer_id, "unsupported_language", "language outside EN/HI/Hinglish"
        )
        state.outcome = "unsupported_language"
        await self._escalate(state, send, reason="unsupported_language")

    async def _escalate(self, state: SessionState, send: Sender, *, reason: str) -> None:
        """Hand the call to a human (3.1). Emits the full-context frame (PRD §02),
        speaks a hold line, and parks the call in ``waiting_agent`` — it does NOT
        end the call. The WS handler queues the handoff + registers the live
        operator bridge on seeing this state; the operator's replies then come
        back through ``deliver_agent_turn`` until they resolve or the caller drops.
        """
        state.escalated = True
        state.escalation_reason = reason
        if state.outcome == "in_progress":
            state.outcome = "escalated"
        state.status = CallStatus.waiting_agent
        await send(
            ServerMessage.of(
                ServerMessageType.call_state,
                status=CallStatus.escalating,  # FE handoff panel keys off this value
                reason=reason,
                # Full context handoff (PRD §02 Escalation Quality = 100%).
                transcript=[t.model_dump(mode="json") for t in state.transcript],
                crm_context=state.crm_context,
            )
        )
        # Reassure the caller they're being connected (ambient muted for the line).
        await self._send_bot_turn(
            state, send, text=_by_language(_HOLD_FOR_AGENT, state.language),
            tone=Tone.empathetic, intent="handoff", ambient=False,
        )

    async def deliver_agent_turn(self, state: SessionState, send: Sender, *, text: str) -> None:
        """Speak a human operator's typed reply to the caller via the normal TTS
        path (3.1 Phase A). The caller's experience stays voice; the transcript
        records it like any bot turn."""
        await self._send_bot_turn(
            state, send, text=text, tone=Tone.neutral, intent="agent", ambient=False
        )

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
