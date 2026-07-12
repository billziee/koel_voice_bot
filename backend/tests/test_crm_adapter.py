"""Live-CRM adapter contract + signed webhook mirror (6.1)."""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx

from koel.common.config import CommonSettings
from koel.crm import adapters as ad
from koel.crm.adapters import CrmAdapter, mirror_to_webhook


def test_crm_adapter_protocol_declares_the_four_operations():
    ops = set(dir(CrmAdapter))
    assert {"get_customer", "log_interaction", "set_flag", "post_call_summary"} <= ops


async def test_mirror_dormant_without_webhook():
    assert await mirror_to_webhook("flag", "c1", {"flag": "opt_out"}, settings=CommonSettings()) is False


class _Capture:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes, dict]] = []

    async def __aenter__(self) -> _Capture:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def post(self, url: str, content: bytes = b"", headers: dict | None = None):
        self.calls.append((url, content, headers or {}))

        class _R:
            def raise_for_status(self) -> None: ...
        return _R()


async def test_mirror_posts_hmac_signed_body_when_configured(monkeypatch):
    s = CommonSettings(crm_webhook_url="https://crm.test/hook", crm_webhook_key="secret")
    cap = _Capture()
    monkeypatch.setattr(ad, "make_async_client", lambda **kw: cap)

    ok = await mirror_to_webhook("interaction", "cust_1", {"event": "promise_to_pay"}, settings=s)
    assert ok is True
    url, body, headers = cap.calls[0]
    assert url == "https://crm.test/hook"
    # The signature must verify against the body with the shared key.
    expected = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert headers["X-Koel-Signature"] == expected
    # And the body is the event envelope.
    payload = json.loads(body)
    assert payload["event"] == "interaction" and payload["customer_id"] == "cust_1"


async def test_mirror_failure_degrades_to_false(monkeypatch):
    s = CommonSettings(crm_webhook_url="https://crm.test/hook", crm_webhook_key="secret")

    def _boom(**kw):
        raise httpx.ConnectError("crm middleware down")

    monkeypatch.setattr(ad, "make_async_client", _boom)
    assert await mirror_to_webhook("flag", "c1", {"flag": "retry"}, settings=s) is False
