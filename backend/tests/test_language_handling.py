"""Bot's configured language is authoritative (fixes: English bot → Hinglish,
call ends after one turn on STT mis-detection)."""

from __future__ import annotations

from koel.common.schemas import Language, Tone
from koel.nlp.schemas import Intent, NlpResponse
from koel.stt.schemas import TranscriptResult
from koel.tts.providers import StubOrpheusProvider
from koel.voicebot.config import VoiceBotSettings
from koel.voicebot.pipeline.filler import FillerCache
from koel.voicebot.pipeline.orchestrator import Orchestrator
from koel.voicebot.session.models import CallStatus, SessionState


class _Clients:
    def __init__(self, stt_lang: Language, stt_conf: float = 0.95) -> None:
        self.stt_lang, self.stt_conf = stt_lang, stt_conf
        self.flags: list = []

    async def transcribe(self, request):  # noqa: ANN001
        return TranscriptResult(
            session_id=request.session_id, text="something", is_final=True,
            language_id=self.stt_lang, language_confidence=self.stt_conf,
            speech_duration_ms=600, word_confidence=0.9,
        )

    async def respond(self, ctx):  # noqa: ANN001
        # Echo the language the orchestrator decided to respond in.
        return NlpResponse(session_id=ctx.session_id, response_text=f"lang={ctx.language.value}",
                           intent=Intent.interested, tone=Tone.cheerful)

    async def stream_synthesis(self, request):  # noqa: ANN001
        async for c in StubOrpheusProvider()._stream_by_sentence(request):
            yield c

    async def flag_customer(self, cid, flag, reason=None):  # noqa: ANN001
        self.flags.append((cid, flag))


def _orch(clients, **over) -> Orchestrator:
    settings = VoiceBotSettings(**over)
    return Orchestrator(settings=settings, clients=clients, filler_cache=FillerCache())


def _state(lang: Language) -> SessionState:
    return SessionState(session_id="s", bot_id="b", customer_id="c", use_case="credit_card",
                        language=lang, crm_context={"name": "R"})


async def _run(orch, state, clients):
    sent = []
    async def send(m):  # noqa: ANN001
        sent.append(m)
    stt = await clients.transcribe(type("R", (), {"session_id": "s"})())
    await orch._handle_final_transcript(state, send, stt)
    return sent


async def test_english_bot_stays_english_despite_hinglish_detection(test_db):
    # #3: an English bot must NOT flip to Hinglish because STT guessed Hinglish.
    clients = _Clients(Language.hinglish)
    state = _state(Language.en)
    await _run(_orch(clients), state, clients)
    assert state.language == Language.en  # unchanged
    assert state.transcript[-1].text == "lang=en"  # responded in English


async def test_unsupported_detection_does_not_end_call_by_default(test_db):
    # #2: a foreign STT guess must not escalate/end the call by default.
    clients = _Clients(Language.other, stt_conf=0.99)
    state = _state(Language.hinglish)
    await _run(_orch(clients), state, clients)
    assert not state.escalated
    assert state.status != CallStatus.escalating
    assert ("c", "unsupported_language") not in clients.flags


async def test_adapt_flag_enables_switch(test_db):
    # Opt-in: with the flag on, a hinglish bot may adapt to a Hindi caller.
    clients = _Clients(Language.hi)
    state = _state(Language.hinglish)
    await _run(_orch(clients, adapt_language_to_caller=True), state, clients)
    assert state.language == Language.hi
