"""TTS response cache (roadmap O-3): key correctness, eviction, and the
stream_synthesis cache/replay path (miss → stream+store, hit → local replay,
barge-in partial → never cached)."""

from __future__ import annotations

import httpx

from koel.common.config import CommonSettings
from koel.common.schemas import Language, Tone
from koel.tts.schemas import SynthesisChunk, SynthesizeRequest
from koel.voicebot.clients import ServiceClients
from koel.voicebot.tts_cache import TTS_CLIP_CACHE, TtsClipCache, tts_cache_key


def _req(**over) -> SynthesizeRequest:
    base = dict(session_id="s", text="namaste")
    base.update(over)
    return SynthesizeRequest(**base)


def _chunk(seq: int, size: int = 100) -> SynthesisChunk:
    return SynthesisChunk(session_id="orig", seq=seq, audio_b64="A" * size, duration_ms=50)


# --- key correctness ----------------------------------------------------------

def test_key_ignores_session_id_and_stream():
    assert tts_cache_key(_req(session_id="a")) == tts_cache_key(_req(session_id="b"))
    assert tts_cache_key(_req(stream=True)) == tts_cache_key(_req(stream=False))


def test_key_varies_with_every_audio_affecting_field():
    base = tts_cache_key(_req())
    assert tts_cache_key(_req(text="alvida")) != base
    assert tts_cache_key(_req(language=Language.en)) != base
    assert tts_cache_key(_req(tone=Tone.firm)) != base
    assert tts_cache_key(_req(voice_gender="male")) != base
    assert tts_cache_key(_req(speaking_rate=1.2)) != base
    assert tts_cache_key(_req(ambient=True)) != base
    assert tts_cache_key(_req(ambient_profile="office")) != base
    assert tts_cache_key(_req(telephony=True)) != base


# --- cache mechanics ----------------------------------------------------------

def test_put_get_roundtrip_and_miss():
    cache = TtsClipCache()
    assert cache.get("missing") is None
    cache.put("k", [_chunk(0), _chunk(1)])
    assert [c.seq for c in cache.get("k")] == [0, 1]


def test_empty_and_oversized_clips_are_not_cached():
    cache = TtsClipCache(max_bytes=50)
    cache.put("empty", [])
    cache.put("big", [_chunk(0, size=100)])  # 100 > 50 budget
    assert len(cache) == 0


def test_ttl_expiry_drops_entry_on_access():
    cache = TtsClipCache(max_bytes=10_000, ttl_s=100)
    cache.put("k", [_chunk(0)], now=0.0)
    assert cache.get("k", now=50.0) is not None
    assert cache.get("k", now=101.0) is None  # past TTL
    assert len(cache) == 0


def test_lru_eviction_by_byte_budget():
    cache = TtsClipCache(max_bytes=250, ttl_s=9_999)
    cache.put("a", [_chunk(0, 100)], now=0.0)
    cache.put("b", [_chunk(0, 100)], now=0.0)
    cache.get("a", now=0.0)  # touch → 'a' is now MRU, 'b' is LRU
    cache.put("c", [_chunk(0, 100)], now=0.0)  # 300 > 250 → evict the LRU ('b')
    assert cache.get("a", now=0.0) is not None
    assert cache.get("b", now=0.0) is None
    assert cache.get("c", now=0.0) is not None


# --- stream_synthesis integration --------------------------------------------

def _clients(handler) -> ServiceClients:
    c = ServiceClients(CommonSettings())
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c


def _two_chunk_handler(calls: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] = calls.get("n", 0) + 1
        lines = "\n".join(
            f'{{"session_id":"orig","seq":{i},"audio_b64":"AAAA",'
            f'"is_final":{str(i == 1).lower()},"duration_ms":50}}'
            for i in range(2)
        )
        return httpx.Response(200, text=lines + "\n")

    return handler


async def test_miss_streams_then_identical_line_hits_cache():
    calls: dict = {}
    c = _clients(_two_chunk_handler(calls))

    first = [ch async for ch in c.stream_synthesis(_req(session_id="s1", text="disclosure"))]
    assert calls["n"] == 1 and len(first) == 2  # miss → hit the network once

    # Same spoken line, DIFFERENT session → served from cache, no second network call.
    second = [ch async for ch in c.stream_synthesis(_req(session_id="s2", text="disclosure"))]
    assert calls["n"] == 1  # still 1 → cache hit
    assert [ch.audio_b64 for ch in second] == [ch.audio_b64 for ch in first]
    assert all(ch.session_id == "s2" for ch in second)  # replay stamped to new session
    await c.aclose()


async def test_barge_in_partial_stream_is_not_cached():
    calls: dict = {}
    c = _clients(_two_chunk_handler(calls))
    req = _req(session_id="s1", text="reprompt")

    agen = c.stream_synthesis(req)
    first = await agen.__anext__()  # consume only the first chunk...
    await agen.aclose()  # ...then abort, as a barge-in would
    assert first.seq == 0
    assert TTS_CLIP_CACHE.get(tts_cache_key(req)) is None  # partial never cached

    # A subsequent identical line is therefore still a miss (hits the network).
    _ = [ch async for ch in c.stream_synthesis(req)]
    assert calls["n"] == 2
    await c.aclose()
