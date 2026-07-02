"""Sentence-streaming TTS (Tier 2): first-sentence-fast, gapless WAV chunks."""

from __future__ import annotations

import base64

from koel.common.schemas import Language, Tone
from koel.tts.providers import GroqOrpheusProvider, split_sentences
from koel.tts.schemas import SynthesizeRequest


def test_split_sentences_incl_danda():
    assert split_sentences("Ek baat. Doosri baat! Teesri?") == ["Ek baat.", "Doosri baat!", "Teesri?"]
    assert split_sentences("पहला वाक्य। दूसरा वाक्य।") == ["पहला वाक्य।", "दूसरा वाक्य।"]
    assert split_sentences("  ") == []
    assert split_sentences("no terminal punctuation") == ["no terminal punctuation"]


async def test_stream_emits_one_wav_chunk_per_sentence():
    # GroqOrpheus with no reachable API degrades each sentence to the stub WAV,
    # but the streaming shape (one complete WAV per sentence) is what we assert.
    p = GroqOrpheusProvider(api_key="k", model="m", voice="autumn")
    req = SynthesizeRequest(
        session_id="s", text="Pehli baat. Doosri baat. Teesri baat.",
        language=Language.hinglish, tone=Tone.cheerful,
    )
    chunks = [c async for c in p.stream(req)]
    assert len(chunks) == 3
    assert [c.seq for c in chunks] == [0, 1, 2]
    assert chunks[-1].is_final is True and not any(c.is_final for c in chunks[:-1])
    # Every chunk is an independently decodable WAV with a real duration.
    for c in chunks:
        assert base64.b64decode(c.audio_b64)[:4] == b"RIFF"
        assert c.duration_ms > 0
    await p.aclose()


async def test_single_sentence_streams_one_final_chunk():
    p = GroqOrpheusProvider(api_key="k", model="m", voice="autumn")
    req = SynthesizeRequest(session_id="s", text="Bas ek baat.", language=Language.en, tone=Tone.neutral)
    chunks = [c async for c in p.stream(req)]
    assert len(chunks) == 1 and chunks[0].is_final is True
    await p.aclose()
