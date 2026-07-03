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
    def __init__(self, stt_lang: Language, stt_conf: float = 0.95, text: str = "something") -> None:
        self.stt_lang, self.stt_conf, self.text = stt_lang, stt_conf, text
        self.flags: list = []

    async def transcribe(self, request):  # noqa: ANN001
        return TranscriptResult(
            session_id=request.session_id, text=self.text, is_final=True,
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


async def test_explicit_english_request_sticks(test_db):
    # A caller saying "continue in English" locks English even on a Hinglish bot,
    # even with STT still guessing Hinglish (explicit request beats STT guess).
    clients = _Clients(Language.hinglish, text="Please continue in English from now on")
    state = _state(Language.hinglish)
    await _run(_orch(clients), state, clients)
    assert state.language == Language.en
    assert state.transcript[-1].text == "lang=en"


async def test_greeting_uses_bot_language_not_hinglish(test_db):
    # An English bot with no template must greet in English — not the old hardcoded
    # Hinglish default + "Do minute baat kar sakte hain?" suffix.
    clients = _Clients(Language.en)
    state = _state(Language.en)  # greeting_template defaults to ""
    sent: list = []

    async def send(m):  # noqa: ANN001
        sent.append(m)

    await _orch(clients).greet(state, send)
    greeting = state.transcript[-1].text
    assert "automated call" in greeting.lower() and "two minutes" in greeting.lower()
    assert "bol rahi hoon" not in greeting and "Do minute" not in greeting


async def test_hindi_greeting_is_devanagari(test_db):
    clients = _Clients(Language.hi)
    state = _state(Language.hi)
    sent: list = []

    async def send(m):  # noqa: ANN001
        sent.append(m)

    await _orch(clients).greet(state, send)
    greeting = state.transcript[-1].text
    assert "स्वचालित" in greeting  # Devanagari disclosure — Magpie voices this natively
    assert "Do minute" not in greeting and "automated call" not in greeting.lower()


async def test_hinglish_greeting_is_devanagari(test_db):
    # Hinglish is voiced by Magpie too, which only renders Devanagari cleanly — the
    # old romanized default came out as gibberish. Greeting must be Devanagari now.
    clients = _Clients(Language.hinglish)
    state = _state(Language.hinglish)
    sent: list = []

    async def send(m):  # noqa: ANN001
        sent.append(m)

    await _orch(clients).greet(state, send)
    greeting = state.transcript[-1].text
    assert "नमस्ते" in greeting  # Devanagari
    assert "bol rahi hoon" not in greeting and "Do minute" not in greeting  # no romanized Hindi


def test_requested_language_detection():
    from koel.voicebot.pipeline.orchestrator import _requested_language

    assert _requested_language("please continue in English") == Language.en
    assert _requested_language("Hindi mein baat karo") == Language.hi
    assert _requested_language("aap English bol sakte hain?") == Language.en
    # Not a language request → no switch.
    assert _requested_language("yes I am interested") is None
    assert _requested_language("annual fee kitni hai") is None
