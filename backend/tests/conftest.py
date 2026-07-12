"""Shared fixtures for integration tests.

Keeps everything in-process — no real Redis or Postgres:
  - ``test_db``    : in-memory SQLite engine, with ``get_sessionmaker`` patched so
                     ``session_scope`` / ``persist_call`` write to it.
  - ``FakeStore``  : in-memory stand-in for ``RedisSessionStore``.
  - ``FakeClients``: stand-in for ``ServiceClients`` (CRM + AI fan-out).
  - ``app_client`` : the VB_Svc app over httpx ASGITransport with ``get_runtime``
                     overridden to inject the fakes.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from koel.common.auth import issue_token
from koel.crm.schemas import CustomerRecord
from koel.db.models import (
    BotConfig,
    BotConfigAudit,
    CallLog,
    Campaign,
    ConsentRecord,
    CrmCustomer,
    DialAttempt,
    EscalationRequest,
    ExportJob,
    InteractionLogRow,
    Org,
    TranscriptTurn,
    UseCase,
)
from koel.tts.providers import StubOrpheusProvider
from koel.voicebot.config import VoiceBotSettings
from koel.voicebot.pipeline.bargein import BargeInClassifier
from koel.voicebot.pipeline.filler import FillerCache
from koel.voicebot.pipeline.orchestrator import Orchestrator
from koel.voicebot.tts_cache import TTS_CLIP_CACHE, TTS_RESULT_CACHE


@pytest.fixture(autouse=True)
def _clear_tts_cache():
    """The TTS caches (O-3) are process-wide singletons — the streaming chunk cache
    and the whole-clip synthesize cache; clear both around every test so a clip
    cached by one test can't produce a surprise hit in another."""
    TTS_CLIP_CACHE.clear()
    TTS_RESULT_CACHE.clear()
    yield
    TTS_CLIP_CACHE.clear()
    TTS_RESULT_CACHE.clear()

# Tables we can create on SQLite (excludes KnowledgeChunk — pgvector Vector type).
_SQLITE_TABLES = [
    Org.__table__,
    CrmCustomer.__table__,
    BotConfig.__table__,
    CallLog.__table__,
    TranscriptTurn.__table__,
    InteractionLogRow.__table__,
    BotConfigAudit.__table__,
    EscalationRequest.__table__,
    ExportJob.__table__,
    ConsentRecord.__table__,
    Campaign.__table__,
    DialAttempt.__table__,
]


@pytest.fixture
async def test_db(monkeypatch):
    """In-memory SQLite bound into koel.db.session for the duration of a test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: CallLog.metadata.create_all(c, tables=_SQLITE_TABLES))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("koel.db.session.get_sessionmaker", lambda: maker)
    yield maker
    await engine.dispose()


class FakeStore:
    """In-memory RedisSessionStore stand-in."""

    def __init__(self) -> None:
        self.data: dict[str, object] = {}
        self.deleted: list[str] = []

    async def save(self, state) -> None:  # noqa: ANN001
        self.data[state.session_id] = state

    async def load(self, session_id: str):
        return self.data.get(session_id)

    async def begin_grace(self, session_id: str) -> bool:
        return session_id in self.data

    async def restore_active_ttl(self, session_id: str) -> bool:
        return session_id in self.data

    async def delete(self, session_id: str) -> None:
        self.deleted.append(session_id)
        self.data.pop(session_id, None)

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        pass


class FakeClients:
    """ServiceClients stand-in. Records CRM flags; returns a canned customer."""

    def __init__(self, customer: CustomerRecord | None = None) -> None:
        self.customer = customer
        self.flags: list[tuple[str, str, str | None]] = []
        self.call_summaries: list[tuple[str, dict]] = []

    async def get_customer(self, customer_id: str) -> CustomerRecord:
        if self.customer is None:
            raise AssertionError("no customer configured on FakeClients")
        return self.customer

    async def flag_customer(self, customer_id: str, flag: str, reason: str | None = None) -> None:
        self.flags.append((customer_id, flag, reason))

    async def add_call_summary(self, customer_id: str, **summary) -> None:
        self.call_summaries.append((customer_id, summary))

    async def synthesize(self, request):  # noqa: ANN001
        return await StubOrpheusProvider().synthesize(request)

    async def stream_synthesis(self, request):  # noqa: ANN001 — sentence-streaming stand-in
        async for chunk in StubOrpheusProvider()._stream_by_sentence(request):
            yield chunk

    async def respond(self, ctx):  # noqa: ANN001 — overridden per-test when needed
        raise NotImplementedError

    async def transcribe(self, request):  # noqa: ANN001
        raise NotImplementedError

    async def stream_transcribe(self, request):  # noqa: ANN001 — O-6 ws-mode STT stand-in
        # Mirror the real client's batch-fallback shape: yield one final result
        # from transcribe(). Subclasses that only override transcribe() get a
        # working stream_transcribe for free; ws-mode tests override this directly.
        yield await self.transcribe(request)

    async def warm_up(self) -> None:  # O-1 session warm-up — no-op in tests
        pass

    async def aclose(self) -> None:
        pass


def make_runtime(*, store: FakeStore, clients: FakeClients, settings: VoiceBotSettings):
    """Assemble a Runtime around the fakes (real bargein/filler/orchestrator)."""
    from koel.voicebot.deps import Runtime

    filler = FillerCache()
    return Runtime(
        settings=settings,
        store=store,
        clients=clients,
        bargein=BargeInClassifier(backchannel_max_speech_ms=settings.backchannel_max_speech_ms),
        filler=filler,
        orchestrator=Orchestrator(settings=settings, clients=clients, filler_cache=filler),
    )


@pytest.fixture
def access_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token('demo', kind='access', role='operator')}"}


@pytest.fixture
async def app_client(test_db):
    """(client, runtime) — the VB app with get_runtime overridden to the fakes.

    The test can mutate ``runtime.settings`` / ``runtime.clients.customer`` and
    seed rows into ``test_db`` before issuing requests.
    """
    from koel.voicebot.deps import get_runtime
    from koel.voicebot.main import app

    store = FakeStore()
    clients = FakeClients()
    runtime = make_runtime(store=store, clients=clients, settings=VoiceBotSettings())
    app.dependency_overrides[get_runtime] = lambda: runtime

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, runtime
    app.dependency_overrides.clear()


async def seed_bot(maker, **overrides) -> str:
    """Insert a BotConfig row into the test DB; return its id."""
    fields = dict(
        id="bot_test",
        name="Test Bot",
        use_case=UseCase.credit_card,
        language="hinglish",
        default_tone="cheerful",
        rag_enabled=True,
        rag_index="cc_hdfc",
        ambient_profile="callfloor",
        greeting_template="",
        active=True,
        config={},
    )
    fields.update(overrides)
    async with maker() as s:
        s.add(BotConfig(**fields))
        await s.commit()
    return fields["id"]
