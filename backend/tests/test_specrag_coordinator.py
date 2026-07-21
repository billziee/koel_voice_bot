"""SpeculativeRagCoordinator (O-5) — the fire/resolve/cancel state machine
extracted out of ws/handler.py so a second transport (telephony) can reuse it.
"""

from __future__ import annotations

import asyncio

from koel.voicebot.config import get_voicebot_settings
from koel.voicebot.pipeline.specrag import SpeculativeRagCoordinator


def _settings(**over):
    s = get_voicebot_settings()
    return s.model_copy(update={"speculative_rag_enabled": True, "spec_retrieval_await_ms": 500, **over})


class _State:
    def __init__(self, *, rag_enabled=True, rag_index="idx1") -> None:
        self.rag_enabled = rag_enabled
        self.rag_index = rag_index


class _FakeClients:
    def __init__(self, chunks=None, *, delay=0.0, error=False) -> None:
        self._chunks = chunks if chunks is not None else ["chunk-a"]
        self._delay = delay
        self._error = error
        self.calls: list = []

    async def retrieve(self, ctx):  # noqa: ANN001
        self.calls.append(ctx)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error:
            raise RuntimeError("retrieval broke")
        return self._chunks


class _FakeOrchestrator:
    def __init__(self, *, fail_context_build=False) -> None:
        self._fail = fail_context_build

    def build_speculative_context(self, state, query):  # noqa: ANN001
        if self._fail:
            raise RuntimeError("context build broke")
        return {"query": query}


def _coordinator(clients=None, orch=None, state=None, **settings_over):
    return SpeculativeRagCoordinator(
        settings=_settings(**settings_over),
        state=state or _State(),
        clients=clients or _FakeClients(),
        orchestrator=orch or _FakeOrchestrator(),
    )


async def test_start_then_resolve_reuses_chunks_when_not_diverged():
    clients = _FakeClients(["a", "b"])
    coord = _coordinator(clients)

    coord.start("what is my emi")
    result = await coord.resolve("what is my emi this month")

    assert result == ["a", "b"]
    assert len(clients.calls) == 1


async def test_resolve_discards_chunks_on_divergence():
    clients = _FakeClients(["a"])
    coord = _coordinator(clients)

    coord.start("what is my emi")
    result = await coord.resolve("completely unrelated question about refunds")

    assert result is None


async def test_resolve_with_nothing_in_flight_returns_none():
    coord = _coordinator()
    assert await coord.resolve("anything") is None


async def test_start_skipped_when_kill_switch_off():
    clients = _FakeClients()
    coord = _coordinator(clients, speculative_rag_enabled=False)

    coord.start("held text")
    await asyncio.sleep(0)  # let any (incorrectly) scheduled task run

    assert clients.calls == []
    assert await coord.resolve("held text") is None


async def test_start_skipped_when_rag_disabled_on_bot():
    clients = _FakeClients()
    coord = _coordinator(clients, state=_State(rag_enabled=False))

    coord.start("held text")
    await asyncio.sleep(0)

    assert clients.calls == []


async def test_start_skipped_when_no_rag_index():
    clients = _FakeClients()
    coord = _coordinator(clients, state=_State(rag_index=None))

    coord.start("held text")
    await asyncio.sleep(0)

    assert clients.calls == []


async def test_start_skipped_on_blank_text():
    clients = _FakeClients()
    coord = _coordinator(clients)

    coord.start("   ")
    await asyncio.sleep(0)

    assert clients.calls == []


async def test_start_skipped_when_client_has_no_retrieve_method():
    class _NoRetrieve:
        pass

    coord = _coordinator(clients=_NoRetrieve())
    coord.start("held text")
    await asyncio.sleep(0)
    # No crash, and nothing to resolve.
    assert await coord.resolve("held text") is None


async def test_start_skipped_when_context_build_fails():
    clients = _FakeClients()
    coord = _coordinator(clients, orch=_FakeOrchestrator(fail_context_build=True))

    coord.start("held text")
    await asyncio.sleep(0)

    assert clients.calls == []


async def test_a_second_start_cancels_the_first():
    clients = _FakeClients(delay=0.2)
    coord = _coordinator(clients)

    coord.start("first held text")
    coord.start("second held text")  # supersedes — first task is cancelled

    result = await coord.resolve("second held text continues")
    assert result == ["chunk-a"]
    # Only the second retrieval ever got a chance to actually run to completion.
    assert len(clients.calls) == 1


async def test_resolve_times_out_on_a_slow_retrieval():
    clients = _FakeClients(delay=0.05)
    coord = _coordinator(clients, spec_retrieval_await_ms=1)

    coord.start("held text")
    result = await coord.resolve("held text continues")

    assert result is None


async def test_resolve_returns_none_on_retrieval_error():
    clients = _FakeClients(error=True)
    coord = _coordinator(clients)

    coord.start("held text")
    result = await coord.resolve("held text continues")

    assert result is None


async def test_cancel_drops_in_flight_retrieval():
    clients = _FakeClients(delay=1.0)
    coord = _coordinator(clients)

    coord.start("held text")
    coord.cancel()

    assert await coord.resolve("held text") is None


async def test_empty_chunks_resolve_to_none():
    clients = _FakeClients([])
    coord = _coordinator(clients)

    coord.start("held text")
    result = await coord.resolve("held text continues")

    assert result is None
