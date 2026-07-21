"""PSTN warm transfer (3.1 Phase B): live-call redirect + operator whisper call
into a shared conference, via httpx.MockTransport (no real Twilio calls)."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx

from koel.voicebot.pipeline.warm_transfer import start_warm_transfer


async def test_skips_when_unconfigured():
    started = await start_warm_transfer(
        call_sid="CA1", operator_phone="+911111111111", account_sid="", auth_token="", from_number=""
    )
    assert started is False


async def test_redirects_customer_call_and_originates_operator_call():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"sid": "CAnew"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    started = await start_warm_transfer(
        call_sid="CA123",
        operator_phone="+911111111111",
        account_sid="AC1",
        auth_token="tok",
        from_number="+10000000000",
        client=client,
    )
    await client.aclose()

    assert started is True
    assert len(requests) == 2

    # First request: redirect the LIVE customer call (URL path carries its CallSid).
    redirect_req = requests[0]
    assert redirect_req.url.path.endswith("/Calls/CA123.json")
    redirect_fields = parse_qs(redirect_req.read().decode())
    assert "koel-escalation-CA123" in redirect_fields["Twiml"][0]
    assert "<Conference>" in redirect_fields["Twiml"][0]

    # Second request: a NEW outbound call to the operator, same conference name.
    operator_req = requests[1]
    assert operator_req.url.path.endswith("/Calls.json")
    operator_fields = parse_qs(operator_req.read().decode())
    assert operator_fields["To"] == ["+911111111111"]
    assert operator_fields["From"] == ["+10000000000"]
    assert "koel-escalation-CA123" in operator_fields["Twiml"][0]
    assert "<Say>" in operator_fields["Twiml"][0]  # the whisper


async def test_redirect_failure_never_originates_the_operator_call():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(404)  # customer call already ended

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    started = await start_warm_transfer(
        call_sid="CA_gone",
        operator_phone="+911111111111",
        account_sid="AC1",
        auth_token="tok",
        from_number="+10000000000",
        client=client,
    )
    await client.aclose()

    assert started is False
    assert len(requests) == 1  # never got to originating the operator call


async def test_operator_call_failure_is_reported_false():
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(201, json={"sid": "CA123"})  # redirect succeeds
        return httpx.Response(500)  # operator call fails

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    started = await start_warm_transfer(
        call_sid="CA123",
        operator_phone="+911111111111",
        account_sid="AC1",
        auth_token="tok",
        from_number="+10000000000",
        client=client,
    )
    await client.aclose()

    assert started is False
    assert call_count == 2
