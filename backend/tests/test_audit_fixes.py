"""Tests for audit-hardening fixes: PII masking + service-principal authZ."""

import pytest

from koel.common.auth import (
    TokenClaims,
    decode_token,
    issue_token,
    require_service_principal,
)
from koel.common.config import CommonSettings
from koel.common.errors import ApiError
from koel.common.logging import mask_account, mask_name

SETTINGS = CommonSettings(jwt_secret="test-secret")


def test_mask_name_initials():
    assert mask_name("Rahul Sharma") == "R. S."
    assert mask_name("") == ""
    assert mask_name(None) == ""


def test_mask_account_keeps_last_four():
    assert mask_account("cust_cc_001") == "*******_001"
    assert mask_account("+91-90000-00001") == "***********0001"
    assert mask_account("abc") == "abc"  # nothing to mask under 4 chars
    assert mask_account(None) == ""


async def test_service_principal_rejects_operator_token():
    claims = TokenClaims(sub="demo", kind="access", role="operator", exp=0, iat=0, jti="x")
    with pytest.raises(ApiError) as exc:
        await require_service_principal(claims)
    assert exc.value.status_code == 403
    assert exc.value.code == "forbidden"


async def test_service_principal_accepts_service_token():
    claims = TokenClaims(sub="voicebot_svc", kind="access", role="service", exp=0, iat=0, jti="x")
    assert (await require_service_principal(claims)).role == "service"


def test_service_token_round_trips_with_service_role():
    token = issue_token("voicebot_svc", kind="access", role="service", settings=SETTINGS)
    claims = decode_token(token, expected_kind="access", settings=SETTINGS)
    assert claims.role == "service"
