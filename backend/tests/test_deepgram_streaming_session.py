"""DeepgramStreamingSession — long-lived continuous-partials STT (O-6 Wave 2).

All hermetic — NO live key. ``websockets.asyncio.client.connect`` is monkeypatched
to a fake awaitable that yields a fake socket. The fake accepts binary PCM +
``CloseStream`` sends and replays scripted Deepgram JSON frames (interim
``is_final:false`` partials, then an ``is_final:true`` final, then ``Metadata``).

Pinned guarantees:
1. Interim frames fire ``on_partial(text)`` per interim; the final fires
   ``on_final(TranscriptResult)`` once with the full transcript.
2. The verified URL/header contract (encoding=linear16, interim_results=true,
   ``Authorization: Token``) and the CloseStream flush on ``finish``.
3. PCM frames are batched to ~``batch_ms`` before being sent (no per-frame churn).
4. A mid-stream WS error ends the session cleanly — ``finish``/``aclose`` never
   raise into the caller; ``start`` surfaces a connect failure (caller falls back).
"""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets.asyncio.client as ws_client

from koel.common.schemas import Language
from koel.stt.schemas import TranscriptResult
from koel.stt.streaming_session import DeepgramStreamingSession

# --- fakes --------------------------------------------------------------------


class _FakeConnect:
    """Awaitable a monkeypatched ``connect`` returns → yields the fake socket
    (``connect(...)`` is awaited in ``DeepgramStreamingSession.start``)."""

    def __init__(self, ws: _FakeDeepgramWS) -> None:
        self._ws = ws

    def __await__(self):  # noqa: ANN204
        async def _c() -> _FakeDeepgramWS:
            return self._ws

        return _c().__await__()


class _FakeDeepgramWS:
    """Records sends; replays scripted frames from ``recv``. A ``"__boom__"``
    sentinel frame makes ``recv`` raise (simulate a mid-stream socket error)."""

    def __init__(self, frames: list[str]) -> None:
        self._frames = list(frames)
        self.binary_sends: list[bytes] = []
        self.text_sends: list[str] = []
        self.closed = False

    async def send(self, data: str | bytes) -> None:
        (self.text_sends if isinstance(data, str) else self.binary_sends).append(data)  # type: ignore[arg-type]

    async def recv(self) -> str:
        if self._frames:
            frame = self._frames.pop(0)
            if frame == "__boom__":
                raise RuntimeError("socket exploded mid-stream")
            return frame
        # No more scripted frames → block until the reader is cancelled. Tests
        # always end the stream with a Metadata frame, so this is never reached
        # on the happy path (keeps a stray recv from spinning).
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def close(self) -> None:
        self.closed = True


def _results(text: str, is_final: bool, *, speech_final: bool = False) -> str:
    return json.dumps(
        {
            "type": "Results",
            "channel": {"alternatives": [{"transcript": text, "confidence": 0.97}]},
            "is_final": is_final,
            "speech_final": speech_final,
            "duration": 1.5,
        }
    )


def _metadata() -> str:
    return json.dumps({"type": "Metadata", "request_id": "r"})


class _Recorder:
    def __init__(self) -> None:
        self.items: list = []

    async def __call__(self, item) -> None:  # noqa: ANN001
        self.items.append(item)


def _session(fake: _FakeDeepgramWS, monkeypatch, **kw) -> DeepgramStreamingSession:  # noqa: ANN001, ANN003
    captured: dict = {}

    def fake_connect(url, **kwargs):  # noqa: ANN001, ANN003
        captured["url"] = url
        captured["headers"] = kwargs.get("additional_headers")
        return _FakeConnect(fake)

    monkeypatch.setattr(ws_client, "connect", fake_connect)
    partials = kw.pop("on_partial", _Recorder())
    finals = kw.pop("on_final", _Recorder())
    base = dict(
        api_key="k",
        model="nova-3",
        sample_rate=16000,
        on_partial=partials,
        on_final=finals,
        session_id="s",
        language=Language.hinglish,
        recv_timeout_s=2.0,
        batch_ms=0,  # flush every frame so binary_sends is deterministic
    )
    sess = DeepgramStreamingSession(**{**base, **kw})  # type: ignore[arg-type]
    sess._captured = captured  # type: ignore[attr-defined]  # test-only handle
    sess._partials = partials  # type: ignore[attr-defined]
    sess._finals = finals  # type: ignore[attr-defined]
    return sess


# --- 1. happy path: interims → final -----------------------------------------


async def test_partials_fire_per_interim_and_one_final(monkeypatch):
    fake = _FakeDeepgramWS(
        [
            _results("I want", False),
            _results("I want to check", False),
            _results("I want to check my balance.", True),
            _metadata(),
        ]
    )
    sess = _session(fake, monkeypatch)

    await sess.start()
    await sess.send_frame(b"\x01\x02" * 200)
    await sess.finish()

    partials = sess._partials.items  # type: ignore[attr-defined]
    finals = sess._finals.items  # type: ignore[attr-defined]
    assert partials == ["I want", "I want to check"]
    assert len(finals) == 1
    final = finals[0]
    assert isinstance(final, TranscriptResult)
    assert final.text == "I want to check my balance."
    assert final.is_final is True
    assert final.language_id == Language.hinglish  # pinned bot language carried
    assert final.word_confidence == 0.97
    assert final.speech_duration_ms == 1500  # from Deepgram's `duration`


async def test_speech_final_also_finalizes(monkeypatch):
    # A frame with speech_final=true (but is_final possibly unset) still finalizes.
    fake = _FakeDeepgramWS(
        [
            _results("haan", False),
            _results("haan boliye", False, speech_final=True),
            _metadata(),
        ]
    )
    sess = _session(fake, monkeypatch)

    await sess.start()
    await sess.send_frame(b"\x00\x01" * 100)
    await sess.finish()

    assert sess._partials.items == ["haan"]  # type: ignore[attr-defined]
    finals = sess._finals.items  # type: ignore[attr-defined]
    assert len(finals) == 1 and finals[0].text == "haan boliye"


async def test_empty_interim_frames_are_skipped(monkeypatch):
    fake = _FakeDeepgramWS(
        [
            _results("", False),  # silence interim → not surfaced
            _results("hello", True),
            _metadata(),
        ]
    )
    sess = _session(fake, monkeypatch)

    await sess.start()
    await sess.finish()

    assert sess._partials.items == []  # type: ignore[attr-defined]
    assert [f.text for f in sess._finals.items] == ["hello"]  # type: ignore[attr-defined]


# --- 2. verified protocol/URL/header + CloseStream flush ----------------------


async def test_url_and_header_follow_verified_contract(monkeypatch):
    fake = _FakeDeepgramWS([_results("x", True), _metadata()])
    sess = _session(fake, monkeypatch)

    await sess.start()
    await sess.send_frame(b"\x01\x02" * 200)
    await sess.finish()

    url = sess._captured["url"]  # type: ignore[attr-defined]
    assert url.startswith("wss://api.deepgram.com/v1/listen?")
    assert "model=nova-3" in url
    assert "encoding=linear16" in url
    assert "sample_rate=16000" in url
    assert "interim_results=true" in url
    assert sess._captured["headers"] == {"Authorization": "Token k"}  # type: ignore[attr-defined]

    # PCM streamed as binary; CloseStream flush is the last text frame sent.
    assert fake.binary_sends, "PCM must be streamed as binary WS frames"
    assert json.loads(fake.text_sends[-1]) == {"type": "CloseStream"}
    assert fake.closed is True  # aclose() closed the socket


# --- 3. frame batching --------------------------------------------------------


async def test_frames_batched_until_threshold(monkeypatch):
    fake = _FakeDeepgramWS([_metadata()])
    # 16 kHz * 2 bytes * 100 ms = 3200-byte batch window.
    sess = _session(fake, monkeypatch, batch_ms=100, on_partial=None)

    await sess.start()
    await sess.send_frame(b"\x00" * 1000)  # < 3200 → buffered, not yet sent
    assert fake.binary_sends == []
    await sess.send_frame(b"\x00" * 2500)  # 3500 ≥ 3200 → one flush
    assert len(fake.binary_sends) == 1
    assert len(fake.binary_sends[0]) == 3500

    await sess.finish()  # flushes any remainder (none) + CloseStream


# --- 4. defensive: errors never raise into the caller -------------------------


async def test_midstream_ws_error_ends_cleanly_without_raising(monkeypatch):
    fake = _FakeDeepgramWS(
        [
            _results("partial before the crash", False),
            "__boom__",  # recv raises here → reader logs + stops, never propagates
        ]
    )
    sess = _session(fake, monkeypatch)

    await sess.start()
    await sess.send_frame(b"\x01\x02" * 50)
    # finish() must NOT raise even though the reader hit a socket error.
    await sess.finish()

    assert sess._partials.items == ["partial before the crash"]  # type: ignore[attr-defined]
    assert sess._finals.items == []  # type: ignore[attr-defined]  # crashed before a final
    assert fake.closed is True


async def test_connect_failure_raises_from_start_for_caller_fallback(monkeypatch):
    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise ConnectionRefusedError("deepgram unreachable")

    monkeypatch.setattr(ws_client, "connect", boom)
    sess = DeepgramStreamingSession(
        api_key="k", sample_rate=16000, on_partial=_Recorder(), on_final=_Recorder()
    )

    # start() surfaces the connect error so the caller degrades to buffered ingest.
    with pytest.raises(ConnectionRefusedError):
        await sess.start()
    # But teardown on a never-started session is still safe.
    await sess.aclose()


async def test_send_and_finish_are_noops_before_start(monkeypatch):
    fake = _FakeDeepgramWS([_metadata()])
    sess = _session(fake, monkeypatch)

    # No start() → no socket. These must be harmless no-ops (never raise).
    await sess.send_frame(b"\x00" * 100)
    await sess.finish()
    await sess.aclose()

    assert fake.binary_sends == []
    assert fake.text_sends == []
