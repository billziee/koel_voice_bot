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
from koel.voicebot.session.models import SessionState


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


async def test_unsupported_language_escalates_when_detected(test_db):
    # Re-enabled by default (safe now STT is pinned): a genuine unsupported language
    # (high-confidence foreign STT id) escalates gracefully to a human.
    clients = _Clients(Language.other, stt_conf=0.99)
    state = _state(Language.hinglish)
    await _run(_orch(clients), state, clients)
    assert state.escalated
    assert state.outcome == "unsupported_language"
    assert ("c", "unsupported_language") in clients.flags


async def test_supported_caller_not_escalated(test_db):
    # A supported-language caller at high confidence is never mis-escalated.
    clients = _Clients(Language.hi, stt_conf=0.99, text="haan boliye")
    state = _state(Language.hinglish)
    await _run(_orch(clients), state, clients)
    assert not state.escalated


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


async def test_hinglish_greeting_is_romanized(test_db):
    # Hinglish is voiced by Orpheus (English TTS) → romanized Latin. Devanagari
    # would be gibberish through Orpheus; only pure-hi bots use Magpie/Devanagari.
    import re

    clients = _Clients(Language.hinglish)
    state = _state(Language.hinglish)
    sent: list = []

    async def send(m):  # noqa: ANN001
        sent.append(m)

    await _orch(clients).greet(state, send)
    greeting = state.transcript[-1].text
    assert "bol rahi hoon" in greeting and "Do minute" in greeting  # romanized Hinglish
    assert not re.search(r"[ऀ-ॿ]", greeting)  # no Devanagari (Orpheus can't voice it)


def test_operational_strings_match_tts_script():
    # Every hardcoded bot utterance for hi MUST be Devanagari (Magpie); hinglish MUST
    # be romanized (Orpheus). Otherwise romanized text leaks into a hi call → Magpie
    # gibberish (the no-input reprompt / filler bug).
    import re

    from koel.voicebot.pipeline.filler import FILLER_PHRASES
    from koel.voicebot.pipeline.silence import CLOSE_TEXT, REPROMPT_TEXT

    deva = re.compile(r"[ऀ-ॿ]")
    assert deva.search(REPROMPT_TEXT[Language.hi]) and deva.search(CLOSE_TEXT[Language.hi])
    assert all(deva.search(p) for p in FILLER_PHRASES[Language.hi])
    # hinglish stays romanized (Orpheus voices it).
    assert not deva.search(REPROMPT_TEXT[Language.hinglish])
    assert not deva.search(CLOSE_TEXT[Language.hinglish])


async def test_ambient_profile_gates_floor_bed(test_db):
    # The Bot Builder / Config toggle sets ambient_profile; "silent" must disable the
    # floor bed (ambient=False on the synth request), "callfloor" must enable it.
    from koel.tts.providers import StubOrpheusProvider

    class Rec(_Clients):
        def __init__(self, lang: Language) -> None:
            super().__init__(lang)
            self.ambient_seen: bool | None = None

        async def stream_synthesis(self, request):  # noqa: ANN001
            self.ambient_seen = request.ambient
            async for c in StubOrpheusProvider()._stream_by_sentence(request):  # noqa: SLF001
                yield c

    async def send(m):  # noqa: ANN001
        pass

    off = Rec(Language.hi)
    s_off = _state(Language.hi)
    s_off.ambient_profile = "silent"
    await _orch(off).greet(s_off, send)
    assert off.ambient_seen is False

    on = Rec(Language.hi)
    s_on = _state(Language.hi)
    s_on.ambient_profile = "callfloor"
    await _orch(on).greet(s_on, send)
    assert on.ambient_seen is True


def test_unsupported_language_detection_survives_force_pinned_stt():
    # With STT force-pinned, an unsupported language transcribes into the bot's
    # language with LOW confidence — that's the signal. Supported-language callers
    # (high confidence) are never mis-flagged; short blips are ignored.
    from koel.stt.schemas import TranscriptResult

    orch = _orch(_Clients(Language.hi))

    def stt(conf: float, dur: int, text: str = "काहीतरी") -> TranscriptResult:
        return TranscriptResult(
            session_id="s", text=text, is_final=True, language_id=Language.hi,
            language_confidence=conf, speech_duration_ms=dur,
        )

    assert orch._is_unsupported_language(stt(0.20, 1500)) is True   # unsupported: low conf, sustained
    assert orch._is_unsupported_language(stt(0.90, 1500, "haan boliye")) is False  # clean Hindi
    assert orch._is_unsupported_language(stt(0.10, 200)) is False   # blip/noise: too short


def test_requested_language_detection():
    from koel.voicebot.pipeline.orchestrator import _requested_language

    assert _requested_language("please continue in English") == Language.en
    assert _requested_language("Hindi mein baat karo") == Language.hi
    assert _requested_language("aap English bol sakte hain?") == Language.en
    # Not a language request → no switch.
    assert _requested_language("yes I am interested") is None
    assert _requested_language("annual fee kitni hai") is None
