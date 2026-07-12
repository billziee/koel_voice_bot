"""A/B script testing — deterministic variant assignment (6.3)."""

from __future__ import annotations

from sqlalchemy import select

from koel.db.models import CallLog
from koel.voicebot.persistence import persist_call
from koel.voicebot.pipeline.experiment import assign_variant
from koel.voicebot.session.models import SessionState, Turn

_EXP = {
    "ab": {
        "experiment_id": "greeting_v2",
        "variants": [
            {"id": "control", "weight": 50, "overrides": {}},
            {"id": "warm", "weight": 50, "overrides": {"greeting_template": "Namaste {name} ji!"}},
        ],
    }
}


def test_no_experiment_is_a_noop():
    assert assign_variant(None, "c1") == (None, {})
    assert assign_variant({}, "c1") == (None, {})
    assert assign_variant({"ab": {"experiment_id": "x", "variants": []}}, "c1") == (None, {})


def test_assignment_is_deterministic_per_customer():
    a = assign_variant(_EXP, "cust_123")
    b = assign_variant(_EXP, "cust_123")
    assert a == b  # same customer → same arm, every time (retry-safe)
    assert a[0] in ("greeting_v2:control", "greeting_v2:warm")


def test_variant_overrides_are_returned_and_scoped():
    # Force the "warm" arm by weighting it 100%.
    exp = {"ab": {"experiment_id": "e", "variants": [
        {"id": "warm", "weight": 1, "overrides": {"greeting_template": "Hi!", "not_allowed": "x"}},
    ]}}
    vid, overrides = assign_variant(exp, "anyone")
    assert vid == "e:warm"
    assert overrides == {"greeting_template": "Hi!"}  # unknown override keys stripped


def test_weight_zero_variant_is_never_chosen():
    exp = {"ab": {"experiment_id": "e", "variants": [
        {"id": "never", "weight": 0, "overrides": {}},
        {"id": "always", "weight": 5, "overrides": {}},
    ]}}
    assert {assign_variant(exp, f"c{i}")[0] for i in range(30)} == {"e:always"}


def test_split_actually_distributes_across_customers():
    arms = {assign_variant(_EXP, f"cust_{i}")[0] for i in range(50)}
    assert arms == {"greeting_v2:control", "greeting_v2:warm"}  # both arms get traffic


async def test_persist_call_stamps_the_variant(test_db):
    s = SessionState(session_id="ab1", bot_id="b", customer_id="c", use_case="collections")
    s.variant_id = "greeting_v2:warm"
    s.add_turn(Turn(index=0, speaker="bot", text="hi"))
    cid = await persist_call(s)
    async with test_db() as db:
        call = (await db.execute(select(CallLog).where(CallLog.id == cid))).scalar_one()
    assert call.metrics["variant_id"] == "greeting_v2:warm"
