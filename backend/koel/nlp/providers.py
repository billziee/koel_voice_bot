"""NLP provider interface + stub generator.

The stub produces deterministic, plausible structured output from keyword
heuristics so the full pipeline runs without Bedrock. The real provider builds
a grounded prompt (CRM context + retrieved RAG chunks + history) and calls
Bedrock with a forced JSON tool schema matching ``NlpResponse``.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod

import httpx

from koel.common.schemas import Tone
from koel.nlp.schemas import Intent, NlpResponse, RetrievedChunk, TurnContext

logger = logging.getLogger(__name__)

# Keyword → intent heuristics (EN / HI / Hinglish), for the demo classifier.
# Order matters: earlier buckets win. Deferral ("busy") is checked before the
# generic affirmatives so "haan, but abhi busy hoon" reads as a deferral.
_INTENT_KEYWORDS: list[tuple[Intent, tuple[str, ...]]] = [
    (Intent.request_human, ("agent", "human", "bande", "specialist", "baat kara")),
    (Intent.opt_out, ("do not call", "dnd", "mat call", "remove me", "unsubscribe")),
    (Intent.busy, ("busy", "vyast", "baad mein", "baad me", "later", "abhi nahi",
                   "abhi time nahi", "call me later", "phir karna", "thodi der baad")),
    (Intent.not_interested, ("not interested", "nahi chahiye", "no thanks", "mat karo")),
    (Intent.promise_to_pay, ("pay", "payment", "kal kar", "promise", "bharunga")),
    (Intent.dispute, ("dispute", "wrong", "galat", "already paid", "kiya hai")),
    (Intent.objection, ("already have", "expensive", "mehnga", "do cards", "fee")),
    (Intent.high_interest, ("yes please", "interested hain", "tell me more", "detail chahiye")),
    (Intent.interested, ("haan", "ok", "okay", "achha", "theek", "sure")),
]

# Default tone per use case (PRD §06).
_DEFAULT_TONE: dict[str, Tone] = {
    "credit_card": Tone.cheerful,
    "personal_loan": Tone.cheerful,
    "insurance": Tone.neutral,
    "collections": Tone.empathetic,
}


class NlpProvider(ABC):
    @abstractmethod
    async def generate(
        self, ctx: TurnContext, retrieved: list[RetrievedChunk]
    ) -> NlpResponse: ...


class StubNlpProvider(NlpProvider):
    """Heuristic intent + templated response. Deterministic for demo/CI."""

    def _classify(self, text: str) -> Intent:
        low = text.lower()
        for intent, keywords in _INTENT_KEYWORDS:
            if any(k in low for k in keywords):
                return intent
        return Intent.unknown

    @staticmethod
    def _last_bot_text(ctx: TurnContext) -> str:
        for turn in reversed(ctx.history):
            if turn.get("speaker") == "bot":
                return turn.get("text", "")
        return ""

    @staticmethod
    def _objection_count(ctx: TurnContext) -> int:
        # How many times the bot has already pitched, so we don't keep pushing.
        return sum(
            1
            for t in ctx.history
            if t.get("speaker") == "bot" and "Interested hain" in t.get("text", "")
        )

    async def generate(
        self, ctx: TurnContext, retrieved: list[RetrievedChunk]
    ) -> NlpResponse:
        intent = self._classify(ctx.customer_text)
        tone = _DEFAULT_TONE.get(ctx.use_case, Tone.neutral)
        name = ctx.crm_context.get("name", "")
        name_prefix = f"{name} ji, " if name else ""
        grounding = retrieved[0].content if retrieved else ""

        escalate = False
        escalation_reason: str | None = None
        end_call = False

        if intent in (Intent.request_human, Intent.high_interest):
            escalate = True
            escalation_reason = "customer requested human / high intent"
            text = "Zaroor. Main abhi aapko ek specialist se connect karti hoon. Ek second hold kijiye."
        elif intent == Intent.opt_out:
            end_call = True
            text = "Bilkul, main aapka number remove kar deti hoon. Aapke time ke liye dhanyavaad."
        elif intent == Intent.busy:
            # The customer wants to defer — acknowledge, offer a callback, and
            # END. Do NOT keep pitching product features (PRD fair-practice).
            tone = Tone.empathetic
            end_call = True
            text = (
                f"Bilkul {name_prefix}koi baat nahi. Main aapko kisi convenient time par "
                "call kar leti hoon. Aapke samay ke liye dhanyavaad!"
            )
        elif intent == Intent.not_interested:
            text = "Koi baat nahi. Aapke samay ke liye dhanyavaad. Achha din ho."
            end_call = True
        elif intent == Intent.objection:
            tone = Tone.empathetic
            # After repeated pitching, stop pushing and offer a softer path.
            if self._objection_count(ctx) >= 1:
                end_call = True
                text = (
                    "Samajh sakti hoon. Main aapko poori detail WhatsApp par bhej deti hoon, "
                    "aap fursat se dekh lijiyega. Dhanyavaad!"
                )
            else:
                fallback = grounding or "Yeh offer aapke liye useful ho sakta hai."
                text = f"Bilkul samajh sakti hoon. {fallback} Interested hain?"
        elif intent == Intent.promise_to_pay:
            tone = Tone.firm if ctx.use_case == "collections" else tone
            text = "Theek hai, main aapko payment link SMS kar deti hoon. Aap kab tak payment kar payenge?"
        elif intent == Intent.interested:
            text = f"{name_prefix}great! Kya main aapko iske benefits aur eligibility detail bata doon?"
        else:
            # Unknown: do NOT read the product spec sheet at the customer. Ask a
            # short clarifying question to keep the conversation two-way.
            text = (
                f"{name_prefix}main samajhna chahti hoon — kya aap is offer ke baare mein "
                "thoda aur jaanna chahenge, ya abhi sahi time nahi hai?"
            )

        # Repetition guard: never send the same line twice in a row (the symptom
        # the customer hit). If we'd repeat, move the conversation forward.
        if text.strip() and text.strip() == self._last_bot_text(ctx).strip() and not end_call:
            end_call = True
            tone = Tone.empathetic
            text = (
                f"{name_prefix}main aapko zyada disturb nahi karna chahti. Detail WhatsApp par "
                "bhej deti hoon — aap fursat se dekh lijiyega. Dhanyavaad!"
            )

        return NlpResponse(
            session_id=ctx.session_id,
            response_text=text,
            intent=intent,
            tone=tone,
            escalate=escalate,
            escalation_reason=escalation_reason,
            end_call=end_call,
            confidence=0.88 if intent is not Intent.unknown else 0.55,
            retrieved=retrieved,
        )


_INTENTS = ", ".join(i.value for i in Intent)
_TONES = ", ".join(t.value for t in Tone)

_SYSTEM_PROMPT = f"""\
You are KOEL, an outbound voice agent for an Indian bank, speaking in warm,
natural Hinglish (code-mixed Hindi/English) on a phone call. You handle credit
card, personal loan, insurance, and collections use cases.

Rules:
- Reply with ONE short, conversational spoken turn (1-3 sentences). No markdown,
  no emojis, no bullet points — this text is spoken aloud by TTS.
- Ground every product fact ONLY in the provided KNOWLEDGE chunks. Never invent
  rates, fees, or features. If you lack a fact, offer to follow up rather than
  guess.
- Be respectful of the customer's time. If they are busy or not interested,
  acknowledge, offer a callback / WhatsApp follow-up, and end the call. Do not
  keep pitching.
- Honour opt-out / do-not-call requests immediately and end the call.
- If the customer asks for a human or shows high purchase intent, escalate.
- Never repeat your previous line verbatim.

You MUST respond with a single JSON object and nothing else, matching exactly:
{{
  "response_text": "<the spoken Hinglish reply>",
  "intent": "<one of: {_INTENTS}>",
  "tone": "<one of: {_TONES}>",
  "escalate": <true|false>,
  "escalation_reason": "<short reason or null>",
  "end_call": <true|false>,
  "confidence": <float 0.0-1.0>
}}
"""


class GroqNlpProvider(NlpProvider):
    """Live NLP via Groq (OpenAI-compatible chat completions).

    Builds a grounded prompt (system rules + CRM context + retrieved RAG chunks
    + rolling history + the customer turn) and forces JSON output that maps onto
    ``NlpResponse``. On any failure it degrades to the deterministic stub so a
    live call never drops — consistent with the best-effort posture elsewhere
    in the pipeline (PRD §12 graceful degradation).
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.groq.com/openai/v1",
        temperature: float = 0.3,
        max_tokens: int = 512,
        timeout_s: float = 8.0,
    ) -> None:
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._client = httpx.AsyncClient(timeout=timeout_s)
        self._fallback = StubNlpProvider()

    @staticmethod
    def _build_user_prompt(ctx: TurnContext, retrieved: list[RetrievedChunk]) -> str:
        parts: list[str] = [
            f"USE CASE: {ctx.use_case}",
            f"LANGUAGE: {ctx.language.value}",
        ]
        if ctx.crm_context:
            crm = ", ".join(f"{k}={v}" for k, v in ctx.crm_context.items() if v not in (None, ""))
            if crm:
                parts.append(f"CUSTOMER (CRM): {crm}")
        if retrieved:
            kb = "\n".join(f"- {c.title}: {c.content}" for c in retrieved)
            parts.append(f"KNOWLEDGE (use only these facts):\n{kb}")
        else:
            parts.append("KNOWLEDGE: (none retrieved — do not state specific product facts)")
        if ctx.history:
            convo = "\n".join(
                f"{t.get('speaker', '?').upper()}: {t.get('text', '')}" for t in ctx.history[-8:]
            )
            parts.append(f"CONVERSATION SO FAR:\n{convo}")
        parts.append(f"CUSTOMER (this turn): {ctx.customer_text}")
        parts.append("Respond with the JSON object now.")
        return "\n\n".join(parts)

    def _parse(self, ctx: TurnContext, retrieved: list[RetrievedChunk], raw: str) -> NlpResponse:
        data = json.loads(raw)
        # Coerce/validate enum-ish fields, falling back to safe defaults.
        try:
            intent = Intent(str(data.get("intent", "")).strip())
        except ValueError:
            intent = Intent.unknown
        try:
            tone = Tone(str(data.get("tone", "")).strip())
        except ValueError:
            tone = _DEFAULT_TONE.get(ctx.use_case, Tone.neutral)
        text = str(data.get("response_text", "")).strip()
        if not text:
            raise ValueError("Groq returned empty response_text")
        confidence = data.get("confidence", 0.8)
        try:
            confidence = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = 0.8
        return NlpResponse(
            session_id=ctx.session_id,
            response_text=text,
            intent=intent,
            tone=tone,
            escalate=bool(data.get("escalate", False)),
            escalation_reason=data.get("escalation_reason") or None,
            end_call=bool(data.get("end_call", False)),
            confidence=confidence,
            retrieved=retrieved,
        )

    async def generate(self, ctx: TurnContext, retrieved: list[RetrievedChunk]) -> NlpResponse:
        payload = {
            "model": self._model,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": self._build_user_prompt(ctx, retrieved)},
            ],
        }
        try:
            resp = await self._client.post(self._url, json=payload, headers=self._headers)
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            return self._parse(ctx, retrieved, content)
        except Exception:  # noqa: BLE001 — never drop a live turn; degrade to the stub
            logger.warning(
                "groq_nlp_generate_failed; falling back to stub",
                extra={"session_id": ctx.session_id},
                exc_info=True,
            )
            return await self._fallback.generate(ctx, retrieved)

    async def aclose(self) -> None:
        await self._client.aclose()
