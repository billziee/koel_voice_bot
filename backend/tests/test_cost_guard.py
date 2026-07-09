"""Per-call cost estimate + budget guard (roadmap O-9)."""

from __future__ import annotations

from koel.common.schemas import Language, Tone
from koel.nlp.schemas import Intent, NlpResponse
from koel.stt.schemas import TranscriptResult
from koel.tts.providers import StubOrpheusProvider
from koel.voicebot.config import VoiceBotSettings
from koel.voicebot.pipeline.cost import estimate_turn_cost_usd
from koel.voicebot.pipeline.filler import FillerCache
from koel.voicebot.pipeline.orchestrator import Orchestrator
from koel.voicebot.session.models import CallStatus, SessionState
from koel.voicebot.ws.protocol import ServerMessageType

# --- pure estimate -----------------------------------------------------------

def test_estimate_is_positive_monotonic_and_deterministic():
    a = estimate_turn_cost_usd(customer_text="haan", bot_text="ok")
    b = estimate_turn_cost_usd(customer_text="haan mujhe aur detail chahiye", bot_text="ok")
    assert a > 0
    assert b > a  # more customer text → higher cost
    assert estimate_turn_cost_usd(customer_text="haan", bot_text="ok") == a  # deterministic
    assert estimate_turn_cost_usd(customer_text="", bot_text="") == 0.0


# --- budget guard via the orchestrator turn loop -----------------------------

class _FakeClients:
    def __init__(self, nlp: NlpResponse):
        self._nlp = nlp

    async def respond(self, ctx):  # noqa: ANN001
        return self._nlp

    async def synthesize(self, request):  # noqa: ANN001
        return await StubOrpheusProvider().synthesize(request)

    async def stream_synthesis(self, request):  # noqa: ANN001
        async for chunk in StubOrpheusProvider()._stream_by_sentence(request):  # noqa: SLF001
            yield chunk

    async def flag_customer(self, customer_id, flag, reason=None):  # noqa: ANN001
        return None


def _state() -> SessionState:
    return SessionState(
        session_id="s", bot_id="b", customer_id="cust_1", use_case="credit_card",
        language=Language.hinglish, crm_context={"name": "Rahul"},
    )


def _nlp() -> NlpResponse:
    # A neutral continuing turn (no end_call / escalate) so only the budget guard
    # can end the call.
    return NlpResponse(
        session_id="s",
        response_text="Great! Yeh card aapke liye perfect hai, benefits bataati hoon.",
        intent=Intent.interested, tone=Tone.cheerful,
    )


def _stt() -> TranscriptResult:
    return TranscriptResult(
        session_id="s", text="haan mujhe interested hoon, detail batao",
        is_final=True, language_id=Language.hinglish, language_confidence=0.95,
    )


async def _drive_turn(settings: VoiceBotSettings, state: SessionState) -> list:
    orch = Orchestrator(settings=settings, clients=_FakeClients(_nlp()), filler_cache=FillerCache())
    sent: list = []
    await orch.handle_final_transcript(state, lambda m: sent.append(m) or _noop(), _stt())
    return sent


async def test_budget_guard_off_by_default_keeps_listening():
    state = _state()
    await _drive_turn(VoiceBotSettings(), state)  # default budget = 0 → disabled
    assert state.status == CallStatus.listening
    assert state.running_cost_usd > 0  # cost is still accumulated (a signal)


async def test_budget_guard_wraps_up_when_ceiling_reached():
    state = _state()
    # A ceiling below one turn's estimated cost, so the guard fires after turn 1.
    sent = await _drive_turn(VoiceBotSettings(session_budget_usd=0.0001), state)
    assert state.status == CallStatus.ended
    assert state.outcome == "declined"
    assert any(m.type == ServerMessageType.call_ended for m in sent)


async def _noop():
    return None
