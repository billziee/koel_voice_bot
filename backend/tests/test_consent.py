"""Evidentiary verbal-consent recording (2.3)."""

from __future__ import annotations

from sqlalchemy import select

from koel.common.schemas import Language
from koel.db.models import ConsentRecord
from koel.voicebot.persistence import persist_call
from koel.voicebot.pipeline.consent import find_consent_turn, is_affirmative
from koel.voicebot.session.models import SessionState, Turn


def test_is_affirmative_across_languages():
    for yes in ["Yes", "haan ji", "theek hai", "I agree", "bilkul", "जी हाँ", "ok"]:
        assert is_affirmative(yes) is True, yes


def test_is_affirmative_rejects_negations_and_noise():
    for no in ["no", "nahi", "no I don't agree", "mat karo", "I do not consent", "hmm let me think"]:
        assert is_affirmative(no) is False, no


def test_find_consent_turn_requires_a_preceding_disclosure():
    # A 'haan' before any bot turn is not consent.
    early = [Turn(index=0, speaker="customer", text="haan")]
    assert find_consent_turn(early) is None
    # After the bot's disclosure, the affirmative customer turn is the anchor.
    flow = [
        Turn(index=0, speaker="bot", text="Yeh ek recorded consent disclosure hai..."),
        Turn(index=1, speaker="customer", text="haan ji, theek hai"),
    ]
    turn = find_consent_turn(flow)
    assert turn is not None and turn.index == 1


def _consent_state(version: str | None, affirm: bool) -> SessionState:
    s = SessionState(session_id="cs1", bot_id="b", customer_id="c", use_case="insurance")
    s.language = Language.hinglish
    s.consent_disclosure_version = version
    s.recording_url = "s3://rec/cs1.wav"
    s.add_turn(Turn(index=0, speaker="bot", text="Disclosure: aapki sehmati chahiye."))
    s.add_turn(Turn(index=1, speaker="customer", text="haan bilkul" if affirm else "nahi"))
    return s


async def test_consent_record_written_when_required_and_affirmed(test_db):
    cid = await persist_call(_consent_state("irdai_v1", affirm=True))
    async with test_db() as db:
        rec = (
            await db.execute(select(ConsentRecord).where(ConsentRecord.call_id == cid))
        ).scalar_one()
    assert rec.disclosure_version == "irdai_v1"
    assert rec.turn_index == 1
    assert rec.consent_text == "haan bilkul"
    assert rec.recording_url == "s3://rec/cs1.wav"


async def test_no_consent_record_without_disclosure_version(test_db):
    cid = await persist_call(_consent_state(None, affirm=True))
    async with test_db() as db:
        rows = (
            await db.execute(select(ConsentRecord).where(ConsentRecord.call_id == cid))
        ).scalars().all()
    assert rows == []


async def test_no_consent_record_when_customer_declines(test_db):
    cid = await persist_call(_consent_state("irdai_v1", affirm=False))
    async with test_db() as db:
        rows = (
            await db.execute(select(ConsentRecord).where(ConsentRecord.call_id == cid))
        ).scalars().all()
    assert rows == []
