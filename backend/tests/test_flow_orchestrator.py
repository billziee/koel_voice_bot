"""6.6 flow interpreter wired into the live turn loop: the current node's prompt
is injected into the NLP context, and the flow advances on the returned intent."""

from __future__ import annotations

from koel.common.schemas import Language
from koel.nlp.schemas import Intent, NlpResponse, Tone
from koel.stt.schemas import TranscriptResult
from koel.voicebot.config import VoiceBotSettings
from koel.voicebot.pipeline.filler import FillerCache
from koel.voicebot.pipeline.orchestrator import Orchestrator
from koel.voicebot.session.models import SessionState
from tests.conftest import FakeClients

_FLOW = {
    "start": "greeting",
    "nodes": {
        "greeting": {"prompt": "NODE_GREETING_PROMPT", "branches": {"interested": "pitch"}},
        "pitch": {"prompt": "NODE_PITCH_PROMPT", "branches": {}},
    },
}


class _CapturingClients(FakeClients):
    """Captures each turn's injected prompt_preamble and returns a scripted intent."""

    def __init__(self, intent: Intent) -> None:
        super().__init__()
        self.preambles: list[str | None] = []
        self._intent = intent

    async def respond(self, ctx):  # noqa: ANN001 — TurnContext
        self.preambles.append(ctx.prompt_preamble)
        return NlpResponse(
            session_id=ctx.session_id, response_text="ok", intent=self._intent, tone=Tone.neutral
        )


def _state() -> SessionState:
    return SessionState(
        session_id="s",
        bot_id="b",
        customer_id="c1",
        use_case="collections",
        prompt_preamble="BASE_PREAMBLE",
        flow=_FLOW,
        flow_node_id="greeting",
    )


def _stt(text: str) -> TranscriptResult:
    return TranscriptResult(session_id="s", text=text, is_final=True, language_id=Language.en)


async def _noop_send(_message) -> None:  # noqa: ANN001 — ServerMessage
    return None


async def test_node_prompt_is_injected_and_flow_advances_on_intent():
    clients = _CapturingClients(Intent.interested)
    orch = Orchestrator(
        settings=VoiceBotSettings(), clients=clients, filler_cache=FillerCache()
    )
    state = _state()

    # Turn 1 from the start node.
    await orch.handle_final_transcript(state, _noop_send, _stt("hello"))

    # The greeting node's prompt augmented (not replaced) the base preamble...
    assert clients.preambles[0] is not None
    assert "NODE_GREETING_PROMPT" in clients.preambles[0]
    assert "BASE_PREAMBLE" in clients.preambles[0]
    # ...and the "interested" intent advanced the flow to "pitch".
    assert state.flow_node_id == "pitch"

    # Turn 2 now injects the pitch node's prompt.
    await orch.handle_final_transcript(state, _noop_send, _stt("tell me more"))
    assert "NODE_PITCH_PROMPT" in (clients.preambles[1] or "")


async def test_unscripted_call_leaves_preamble_untouched():
    clients = _CapturingClients(Intent.interested)
    orch = Orchestrator(
        settings=VoiceBotSettings(), clients=clients, filler_cache=FillerCache()
    )
    state = SessionState(
        session_id="s", bot_id="b", customer_id="c1", use_case="collections",
        prompt_preamble="BASE_PREAMBLE",
    )  # no flow

    await orch.handle_final_transcript(state, _noop_send, _stt("hello"))

    assert clients.preambles[0] == "BASE_PREAMBLE"
    assert state.flow_node_id is None
