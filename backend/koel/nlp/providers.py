"""NLP provider interface + stub generator.

The stub produces deterministic, plausible structured output from keyword
heuristics so the full pipeline runs without Bedrock. The real provider builds
a grounded prompt (CRM context + retrieved RAG chunks + history) and calls
Bedrock with a forced JSON tool schema matching ``NlpResponse``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from koel.common.schemas import Tone
from koel.nlp.schemas import Intent, NlpResponse, RetrievedChunk, TurnContext

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


class BedrockNlpProvider(NlpProvider):  # pragma: no cover - production stub
    def __init__(self, model_id: str, region: str) -> None:
        self.model_id = model_id
        self.region = region

    async def generate(self, ctx: TurnContext, retrieved: list[RetrievedChunk]) -> NlpResponse:
        raise NotImplementedError(
            "Build a grounded prompt and call Bedrock with a forced NlpResponse JSON schema."
        )
