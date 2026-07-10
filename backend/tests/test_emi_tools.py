"""Deterministic EMI calculator (roadmap 2.2)."""

from __future__ import annotations

import pytest

from koel.nlp.tools import emi, emi_breakdown, emi_options, format_inr


def test_emi_matches_known_reference_value():
    # Textbook: ₹1,00,000 @ 12% p.a. over 12 months → ₹8,885/month.
    assert emi(100_000, 12.0, 12) == 8885


def test_zero_rate_is_simple_division():
    assert emi(120_000, 0.0, 12) == 10_000


def test_emi_decreases_as_tenure_lengthens():
    # Same loan, longer tenure → lower monthly outgo (more total interest).
    short = emi(500_000, 10.5, 24)
    long = emi(500_000, 10.5, 60)
    assert short > long


def test_emi_increases_with_rate():
    assert emi(500_000, 14.0, 36) > emi(500_000, 9.0, 36)


@pytest.mark.parametrize(
    "principal,months",
    [(0, 12), (-1, 12), (500_000, 0), (500_000, -6), (500_000, 601)],
)
def test_emi_rejects_junk_inputs(principal, months):
    with pytest.raises(ValueError):
        emi(principal, 10.5, months)


def test_emi_rejects_negative_rate():
    with pytest.raises(ValueError):
        emi(500_000, -1.0, 12)


def test_breakdown_is_internally_consistent():
    b = emi_breakdown(500_000, 10.5, 36)
    assert b["total_payable"] == b["emi"] * 36
    assert b["total_interest"] == b["total_payable"] - 500_000
    assert b["total_interest"] > 0  # a positive-rate loan always costs more than principal


def test_emi_options_covers_standard_tenures_and_is_monotonic():
    opts = emi_options(500_000, 10.5)
    assert set(opts) == {12, 24, 36, 48, 60, 72}
    tenures = sorted(opts)
    emis = [opts[t] for t in tenures]
    # EMI strictly decreases as tenure lengthens.
    assert emis == sorted(emis, reverse=True)


def test_emi_options_skips_bad_tenures_without_failing():
    opts = emi_options(500_000, 10.5, tenures_months=(12, 0, 36, 9999))
    assert set(opts) == {12, 36}  # 0 and 9999 rejected, valid ones survive


def test_emi_options_empty_on_bad_principal():
    assert emi_options(0, 10.5) == {}


def test_format_inr_uses_indian_grouping():
    assert format_inr(500_000) == "₹5,00,000"
    assert format_inr(1_250_000) == "₹12,50,000"
    assert format_inr(16_251) == "₹16,251"
    assert format_inr(900) == "₹900"


# --- Prompt grounding (2.2): the NLP prompt gets server-computed EMI, not scripted ---

_PL_OFFER = {
    "name": "Rahul",
    "pre_approved_amount": 500_000,
    "interest_rate": 10.5,
    "tenure_months": 36,
    "indicative_emi": 16_200,  # the SCRIPTED (approximate) figure we override
}


def test_emi_grounding_quotes_the_correct_not_the_scripted_emi():
    from koel.nlp.providers import emi_grounding

    grounding = emi_grounding(_PL_OFFER)
    assert grounding is not None
    # The mathematically-correct EMI for this offer is ₹16,251 — NOT the seed's 16,200.
    assert format_inr(emi(500_000, 10.5, 36)) in grounding
    assert "₹16,251" in grounding
    assert "16,200" not in grounding  # the scripted approximation must not leak in
    assert "never calculate" in grounding.lower()
    # A what-if table is present so the bot can answer "what about 48 months?".
    assert "48mo=" in grounding


def test_emi_grounding_absent_when_offer_fields_missing():
    from koel.nlp.providers import emi_grounding

    assert emi_grounding({"name": "Rahul"}) is None
    assert emi_grounding({"pre_approved_amount": 500_000}) is None  # no rate/tenure


def test_emi_grounding_survives_junk_field_types():
    from koel.nlp.providers import emi_grounding

    assert emi_grounding(
        {"pre_approved_amount": "not-a-number", "interest_rate": 10.5, "tenure_months": 36}
    ) is None


def test_grounding_only_appears_for_personal_loan_prompts():
    from koel.common.schemas import Language
    from koel.nlp.providers import GroqNlpProvider
    from koel.nlp.schemas import TurnContext

    provider = GroqNlpProvider(api_key="k", model="m", base_url="http://x")

    def _ctx(use_case: str) -> TurnContext:
        return TurnContext(
            session_id="s", bot_id="b", use_case=use_case, language=Language.hinglish,
            customer_text="mujhe EMI batao", crm_context=_PL_OFFER,
        )

    pl_prompt = provider._build_user_prompt(_ctx("personal_loan"), [])
    cc_prompt = provider._build_user_prompt(_ctx("credit_card"), [])
    assert "server-computed" in pl_prompt and "₹16,251" in pl_prompt
    assert "server-computed" not in cc_prompt  # grounding is personal-loan only
