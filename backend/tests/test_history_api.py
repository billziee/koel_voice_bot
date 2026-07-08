"""Call history + transcript replay API (PRD §09 Screen 4)."""

from __future__ import annotations

from datetime import UTC, datetime

from koel.db.models import CallLog, CallOutcome, TranscriptTurn
from koel.voicebot.persistence import persist_call_summary
from tests.conftest import seed_bot


async def _seed_call(maker, call_id="call_1", bot_id="bot_test", outcome=CallOutcome.converted):
    async with maker() as s:
        s.add(CallLog(
            id=call_id, session_id=f"sess_{call_id}", bot_id=bot_id, language="hinglish",
            outcome=outcome, escalated=False, started_at=datetime.now(UTC), duration_ms=42_000,
        ))
        s.add(TranscriptTurn(
            call_id=call_id, turn_index=0, speaker="bot", text="Namaste", tone="cheerful",
        ))
        s.add(TranscriptTurn(
            call_id=call_id, turn_index=1, speaker="customer", text="haan boliye",
        ))
        await s.commit()


async def test_list_calls_returns_seeded_call(app_client, access_headers, test_db):
    client, _ = app_client
    await seed_bot(test_db)
    await _seed_call(test_db)
    r = await client.get("/v1/calls", headers=access_headers)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["outcome"] == "converted"
    assert rows[0]["duration_ms"] == 42_000


async def test_list_calls_filters_by_outcome(app_client, access_headers, test_db):
    client, _ = app_client
    await seed_bot(test_db)
    await _seed_call(test_db, call_id="c_conv", outcome=CallOutcome.converted)
    await _seed_call(test_db, call_id="c_decl", outcome=CallOutcome.declined)
    r = await client.get("/v1/calls?outcome=declined", headers=access_headers)
    assert [row["id"] for row in r.json()] == ["c_decl"]


async def test_get_call_returns_transcript(app_client, access_headers, test_db):
    client, _ = app_client
    await seed_bot(test_db)
    await _seed_call(test_db)
    r = await client.get("/v1/calls/call_1", headers=access_headers)
    assert r.status_code == 200
    body = r.json()
    assert [t["speaker"] for t in body["transcript"]] == ["bot", "customer"]
    assert body["transcript"][0]["tone"] == "cheerful"


async def test_get_missing_call_is_404(app_client, access_headers, test_db):
    client, _ = app_client
    r = await client.get("/v1/calls/nope", headers=access_headers)
    assert r.status_code == 404


async def test_call_summary_persists_and_surfaces_in_detail(app_client, access_headers, test_db):
    # 5.3: the post-call summarizer output is stored on the call and exposed in
    # the detail view. persist_call_summary keys on session_id ("sess_call_1").
    client, _ = app_client
    await seed_bot(test_db)
    await _seed_call(test_db)
    await persist_call_summary("sess_call_1", {
        "interest_shown": True, "objection": "annual fee",
        "sentiment": "positive", "next_action": "send WhatsApp link",
    })
    r = await client.get("/v1/calls/call_1", headers=access_headers)
    assert r.status_code == 200
    assert r.json()["summary"] == {
        "interest_shown": True, "objection": "annual fee",
        "sentiment": "positive", "next_action": "send WhatsApp link",
    }


async def test_call_detail_summary_is_null_when_absent(app_client, access_headers, test_db):
    client, _ = app_client
    await seed_bot(test_db)
    await _seed_call(test_db)  # no summary persisted
    r = await client.get("/v1/calls/call_1", headers=access_headers)
    assert r.status_code == 200 and r.json()["summary"] is None
