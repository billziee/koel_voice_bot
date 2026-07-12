"""Deterministic script-adherence QA scoring (5.5)."""

from __future__ import annotations

from sqlalchemy import select

from koel.db.models import CallLog
from koel.voicebot.persistence import persist_call
from koel.voicebot.pipeline.qa import score_transcript
from koel.voicebot.session.models import SessionState, Turn


def _turns(bot_lines: list[str], cust_intents: tuple[str, ...] = ()) -> list[Turn]:
    turns: list[Turn] = []
    i = 0
    for line in bot_lines:
        turns.append(Turn(index=i, speaker="bot", text=line))
        i += 1
    for intent in cust_intents:
        turns.append(Turn(index=i, speaker="customer", text="ok", intent=intent))
        i += 1
    return turns


def test_qa_skipped_without_rules():
    assert score_transcript(_turns(["hi"]), {}) is None
    assert score_transcript(_turns(["hi"]), None) is None


def test_qa_all_controls_pass():
    rules = {
        "required_disclosures": ["this is an automated call"],
        "prohibited_phrases": ["guaranteed returns"],
        "required_intents": ["interested"],
    }
    turns = _turns(["Namaste, this is an automated call from HDFC."], cust_intents=("interested",))
    q = score_transcript(turns, rules)
    assert q["passed"] is True and q["score"] == 1.0
    assert q["missing_disclosures"] == [] and q["prohibited_hits"] == []


def test_qa_missing_disclosure_and_prohibited_phrase_hard_fail():
    rules = {"required_disclosures": ["automated call"], "prohibited_phrases": ["guaranteed returns"]}
    q = score_transcript(_turns(["Invest now for guaranteed returns!"]), rules)
    assert q["passed"] is False
    assert "automated call" in q["missing_disclosures"]
    assert "guaranteed returns" in q["prohibited_hits"]
    assert q["score"] == 0.0  # 2 checks, 2 violations


def test_qa_missing_intent_lowers_score_but_is_not_a_hard_fail():
    rules = {"required_disclosures": ["automated call"], "required_intents": ["promise_to_pay"]}
    q = score_transcript(_turns(["this is an automated call"], cust_intents=("not_interested",)), rules)
    assert q["passed"] is True  # disclosure made, nothing prohibited
    assert "promise_to_pay" in q["missing_intents"]
    assert q["score"] == 0.5  # 2 checks, 1 (soft) violation


async def test_persist_call_stores_qa_when_rules_present(test_db):
    s = SessionState(session_id="qa1", bot_id="b", customer_id="c", use_case="collections")
    s.qa_rules = {"required_disclosures": ["automated call"], "prohibited_phrases": ["guaranteed"]}
    s.add_turn(Turn(index=0, speaker="bot", text="Namaste, this is an automated call."))
    cid = await persist_call(s)
    async with test_db() as db:
        call = (await db.execute(select(CallLog).where(CallLog.id == cid))).scalar_one()
    assert call.metrics["qa"]["passed"] is True
    assert call.metrics["qa"]["disclosures_met"] == ["automated call"]


async def test_persist_call_omits_qa_without_rules(test_db):
    s = SessionState(session_id="qa2", bot_id="b", customer_id="c", use_case="collections")
    s.add_turn(Turn(index=0, speaker="bot", text="hi"))
    cid = await persist_call(s)
    async with test_db() as db:
        call = (await db.execute(select(CallLog).where(CallLog.id == cid))).scalar_one()
    assert "qa" not in call.metrics
