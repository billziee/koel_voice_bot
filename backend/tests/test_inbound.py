"""Inbound call bootstrap (roadmap 3.2).

Covers the ANI lookup (``resolve_inbound_customer``), the intent-first session
build (``build_inbound_session``), the CRM ``/v1/customers/by-phone`` lookup
route, and the direction-keyed inbound system-prompt selector. Deterministic and
in-process — no live Redis/Postgres/HTTP.
"""

from __future__ import annotations

import pytest

from koel.common.auth import TokenClaims
from koel.crm.schemas import CustomerRecord
from koel.db.models import BotConfig, CrmCustomer, UseCase
from koel.nlp.providers import (
    _INBOUND_SYSTEM_PROMPT,
    _SYSTEM_PROMPT,
    GroqNlpProvider,
    select_system_prompt,
)
from koel.nlp.schemas import TurnContext
from koel.voicebot.inbound import (
    DIRECTION_KEY,
    build_inbound_session,
    resolve_inbound_customer,
)

from .conftest import seed_bot

_CLAIMS = TokenClaims(sub="svc", kind="access", role="service", exp=9_999_999_999, iat=0, jti="j")


# --- Fakes ------------------------------------------------------------------


class _StubCrm:
    """Minimal CrmLookup: returns a canned result (or raises) and records calls."""

    def __init__(
        self, result: CustomerRecord | None = None, *, error: Exception | None = None
    ) -> None:
        self._result = result
        self._error = error
        self.calls: list[tuple[str, str | None]] = []

    async def get_customer_by_phone(
        self, phone: str, *, org_id: str | None = None
    ) -> CustomerRecord | None:
        self.calls.append((phone, org_id))
        if self._error is not None:
            raise self._error
        return self._result


def _customer(**overrides: object) -> CustomerRecord:
    fields: dict[str, object] = dict(
        id="cust_1",
        name="Asha Rao",
        phone="+919812345678",
        language="hi",
        bank="HDFC Bank",
        use_case=UseCase.credit_card,
        dnd=False,
        # One use-case-relevant attribute + one irrelevant one, to prove minimisation.
        attributes={"product": "Regalia", "internal_secret": "should-not-leak"},
    )
    fields.update(overrides)
    return CustomerRecord(**fields)  # type: ignore[arg-type]


# --- resolve_inbound_customer ----------------------------------------------


async def test_resolve_returns_record_for_known_phone():
    # Arrange
    clients = _StubCrm(_customer())

    # Act
    resolved = await resolve_inbound_customer(clients, "+919812345678", org_id="org_a")

    # Assert
    assert resolved is not None
    assert resolved.id == "cust_1"
    assert clients.calls == [("+919812345678", "org_a")]


async def test_resolve_returns_none_for_missing_phone_without_calling_crm():
    # Arrange
    clients = _StubCrm(_customer())

    # Act
    resolved = await resolve_inbound_customer(clients, None)

    # Assert
    assert resolved is None
    assert clients.calls == []  # no ANI → no lookup


async def test_resolve_returns_none_for_unknown_phone():
    # Arrange
    clients = _StubCrm(None)  # CRM says 404 → None

    # Act
    resolved = await resolve_inbound_customer(clients, "+910000000000")

    # Assert
    assert resolved is None


async def test_resolve_never_raises_on_lookup_error():
    # Arrange
    clients = _StubCrm(error=RuntimeError("crm down"))

    # Act — a lookup failure must degrade to an anonymous session, never propagate.
    resolved = await resolve_inbound_customer(clients, "+919812345678")

    # Assert
    assert resolved is None


# --- build_inbound_session --------------------------------------------------


async def _load_bot(test_db, **overrides) -> BotConfig:
    bot_id = await seed_bot(test_db, **overrides)
    async with test_db() as s:
        bot = await s.get(BotConfig, bot_id)
    assert bot is not None
    return bot


async def test_build_inbound_session_threads_resolved_customer(test_db):
    # Arrange
    bot = await _load_bot(test_db, org_id="org_a", use_case=UseCase.credit_card)
    clients = _StubCrm(_customer(language="hi"))

    # Act
    state = await build_inbound_session(clients, bot, caller_phone="+919812345678")

    # Assert — customer resolved and its fields threaded into crm_context.
    assert state.customer_id == "cust_1"
    assert state.crm_context[DIRECTION_KEY] == "incoming"
    assert state.crm_context["name"] == "Asha Rao"
    assert state.crm_context["phone"] == "+919812345678"
    assert state.crm_context["product"] == "Regalia"
    # DPDP minimisation: the irrelevant attribute is dropped.
    assert "internal_secret" not in state.crm_context
    # Resolved customer language wins over the bot default.
    assert state.language.value == "hi"
    # Shared bootstrap fields flow through from the bot.
    assert state.bot_id == bot.id
    assert state.org_id == "org_a"
    assert state.use_case == "credit_card"
    # Lookup was scoped to the bot's org.
    assert clients.calls == [("+919812345678", "org_a")]


async def test_build_inbound_session_anonymous_for_unknown_caller(test_db):
    # Arrange
    bot = await _load_bot(test_db, language="hinglish")
    clients = _StubCrm(None)  # unknown ANI

    # Act
    state = await build_inbound_session(clients, bot, caller_phone="+910000000000")

    # Assert — still built, just anonymous and intent-first.
    assert state.customer_id is None
    assert state.crm_context == {DIRECTION_KEY: "incoming", "phone": "+910000000000"}
    # Falls back to the bot's configured language.
    assert state.language.value == "hinglish"
    assert state.bot_id == bot.id


async def test_build_inbound_session_no_phone_still_marks_inbound(test_db):
    # Arrange
    bot = await _load_bot(test_db)
    clients = _StubCrm(None)

    # Act — a call with no From param at all.
    state = await build_inbound_session(clients, bot, caller_phone=None)

    # Assert
    assert state.customer_id is None
    assert state.crm_context == {DIRECTION_KEY: "incoming"}
    assert clients.calls == []  # missing phone → no CRM call


# --- CRM /v1/customers/by-phone --------------------------------------------


async def _seed_crm_customer(test_db, **overrides) -> None:
    fields = dict(
        id="c1", name="Asha Rao", phone="+919812345678", language="hi",
        bank="HDFC Bank", use_case=UseCase.credit_card, org_id="org_a",
    )
    fields.update(overrides)
    async with test_db() as s:
        s.add(CrmCustomer(**fields))
        await s.commit()


async def test_by_phone_route_resolves_known_number(test_db):
    from koel.crm.main import get_customer_by_phone

    await _seed_crm_customer(test_db)
    record = await get_customer_by_phone("+919812345678", _CLAIMS)
    assert record.id == "c1"
    assert record.phone == "+919812345678"


async def test_by_phone_route_404s_for_unknown_number(test_db):
    from koel.common.errors import ApiError
    from koel.crm.main import get_customer_by_phone

    await _seed_crm_customer(test_db)
    with pytest.raises(ApiError) as exc:
        await get_customer_by_phone("+910000000000", _CLAIMS)
    assert exc.value.status_code == 404


async def test_by_phone_route_is_org_scoped(test_db):
    from koel.common.errors import ApiError
    from koel.crm.main import get_customer_by_phone

    await _seed_crm_customer(test_db, org_id="org_a")
    # Same number, wrong tenant → not found (tenant isolation, AF).
    with pytest.raises(ApiError) as exc:
        await get_customer_by_phone("+919812345678", _CLAIMS, org_id="org_b")
    assert exc.value.status_code == 404
    # Correct tenant resolves.
    record = await get_customer_by_phone("+919812345678", _CLAIMS, org_id="org_a")
    assert record.id == "c1"


# --- Intent-first prompt selector ------------------------------------------


def test_selector_returns_inbound_variant_for_incoming():
    prompt = select_system_prompt("incoming")
    assert prompt is _INBOUND_SYSTEM_PROMPT
    # The inbound variant is the outbound prompt PLUS the intent-first addendum.
    assert prompt.startswith(_SYSTEM_PROMPT)
    assert "INBOUND CALL MODE" in prompt


def test_selector_returns_unchanged_outbound_prompt_for_outgoing_and_default():
    # Outbound and any missing/unknown direction return the byte-for-byte
    # unchanged outbound system prompt.
    assert select_system_prompt("outgoing") is _SYSTEM_PROMPT
    assert select_system_prompt(None) is _SYSTEM_PROMPT
    assert select_system_prompt("garbage") is _SYSTEM_PROMPT


def test_payload_uses_inbound_prompt_when_direction_incoming():
    provider = GroqNlpProvider(api_key="k", model="m")
    inbound_ctx = TurnContext(
        session_id="s", bot_id="b", use_case="credit_card", language="hinglish",
        customer_text="hello", crm_context={DIRECTION_KEY: "incoming"},
    )
    outbound_ctx = TurnContext(
        session_id="s", bot_id="b", use_case="credit_card", language="hinglish",
        customer_text="hello", crm_context={},
    )

    inbound_system = provider._payload(inbound_ctx, [], stream=False)["messages"][0]["content"]
    outbound_system = provider._payload(outbound_ctx, [], stream=False)["messages"][0]["content"]

    assert "INBOUND CALL MODE" in inbound_system
    # Outbound is byte-for-byte the unchanged system prompt.
    assert outbound_system == _SYSTEM_PROMPT


def test_build_user_prompt_keeps_direction_out_of_crm_line():
    provider = GroqNlpProvider(api_key="k", model="m")
    ctx = TurnContext(
        session_id="s", bot_id="b", use_case="credit_card", language="hinglish",
        customer_text="hi", crm_context={DIRECTION_KEY: "incoming", "name": "Asha"},
    )
    user_prompt = provider._build_user_prompt(ctx, [])
    assert "CUSTOMER (CRM)" in user_prompt
    assert "name=Asha" in user_prompt
    # The routing key must never appear as a CRM attribute shown to the model.
    assert "direction=" not in user_prompt
