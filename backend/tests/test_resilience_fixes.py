"""Tests for the PRD-validation fixes: per-bot backchannels, ambient audio,
immediate opt-out, and dropped-call finalisation decision logic."""

from __future__ import annotations

import base64

from koel.common.schemas import Language, Tone
from koel.nlp.schemas import Intent, NlpResponse
from koel.stt.schemas import TranscriptResult
from koel.tts.providers import StubOrpheusProvider
from koel.tts.schemas import SynthesizeRequest
from koel.voicebot.config import VoiceBotSettings
from koel.voicebot.pipeline.bargein import BargeInClassifier
from koel.voicebot.pipeline.filler import FillerCache
from koel.voicebot.pipeline.orchestrator import Orchestrator
from koel.voicebot.session.models import CallStatus, SessionState
from koel.voicebot.ws.protocol import ServerMessageType

# --- Per-bot backchannel dictionary (PRD §05) -------------------------------


def test_per_bot_backchannel_word_is_ignored():
    clf = BargeInClassifier(backchannel_max_speech_ms=300)
    # "bilkul" is not in the default dict → without an override it interrupts.
    base = clf.evaluate(partial_text="bilkul", speech_duration_ms=500, language=Language.hinglish)
    assert base.interrupt is True
    # With a per-bot override it is treated as a backchannel → keep talking.
    override = clf.evaluate(
        partial_text="bilkul",
        speech_duration_ms=500,
        language=Language.hinglish,
        extra_backchannels={"bilkul"},
    )
    assert override.interrupt is False
    assert override.reason == "backchannel"


def test_real_interrupt_still_fires_with_overrides():
    clf = BargeInClassifier(backchannel_max_speech_ms=300)
    decision = clf.evaluate(
        partial_text="ruko mujhe ye card nahi chahiye",
        speech_duration_ms=900,
        language=Language.hinglish,
        extra_backchannels={"bilkul"},
    )
    assert decision.interrupt is True


# --- Ambient audio layer (PRD §06) ------------------------------------------


async def test_ambient_audio_is_mixed_when_enabled():
    prov = StubOrpheusProvider()
    silent = await prov.synthesize(
        SynthesizeRequest(session_id="s", text="Namaste ji kaise hain aap", tone=Tone.cheerful)
    )
    ambient = await prov.synthesize(
        SynthesizeRequest(
            session_id="s",
            text="Namaste ji kaise hain aap",
            tone=Tone.cheerful,
            ambient=True,
            ambient_profile="callfloor",
        )
    )
    # Same duration, but the ambient bed is audible (non-zero) vs. pure silence.
    assert ambient.duration_ms == silent.duration_ms
    assert set(base64.b64decode(silent.audio_b64)) == {0}
    assert set(base64.b64decode(ambient.audio_b64)) != {0}


# --- Immediate opt-out (PRD §11 TRAI) ---------------------------------------


class _FakeClients:
    def __init__(self, nlp: NlpResponse):
        self._nlp = nlp
        self.flags: list[tuple[str, str]] = []

    async def respond(self, ctx):  # noqa: ANN001
        return self._nlp

    async def synthesize(self, request):  # noqa: ANN001
        return await StubOrpheusProvider().synthesize(request)

    async def flag_customer(self, customer_id, flag, reason=None):  # noqa: ANN001
        self.flags.append((customer_id, flag))


def _state() -> SessionState:
    return SessionState(
        session_id="s", bot_id="b", customer_id="cust_1", use_case="credit_card",
        language=Language.hinglish, crm_context={"name": "Rahul"},
    )


async def test_opt_out_flags_crm_and_ends_call_immediately():
    nlp = NlpResponse(
        session_id="s",
        response_text="Bilkul, main aapka number remove kar deti hoon.",
        intent=Intent.opt_out,
        tone=Tone.empathetic,
        end_call=False,  # even without end_call, opt-out MUST end the call
    )
    clients = _FakeClients(nlp)
    orch = Orchestrator(
        settings=VoiceBotSettings(), clients=clients, filler_cache=FillerCache()
    )
    sent: list = []
    state = _state()
    stt = TranscriptResult(
        session_id="s", text="mujhe dnd karo dobara call mat karna", is_final=True,
        language_id=Language.hinglish, language_confidence=0.95,
    )
    await orch.handle_final_transcript(state, lambda m: sent.append(m) or _noop(), stt)

    assert ("cust_1", "opt_out") in clients.flags
    assert state.status == CallStatus.ended
    assert any(m.type == ServerMessageType.call_ended for m in sent)


async def _noop():
    return None
