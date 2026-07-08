"""Session warm-up (roadmap O-1): provider pools are primed at session start so
the caller's first turn skips the cold cross-ocean handshake. Everything here is
best-effort — a warm-up failure must never raise into a call path."""

from __future__ import annotations

import httpx

from koel.common.config import CommonSettings
from koel.common.http import make_async_client, warm_connection
from koel.nlp.providers import GroqNlpProvider, StubNlpProvider
from koel.voicebot.clients import ServiceClients

# --- the connection-warming helper -------------------------------------------

async def test_warm_connection_issues_get_to_host():
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["method"] = req.method
        seen["url"] = str(req.url)
        return httpx.Response(405)  # GET on a POST-only path still warms the pool

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await warm_connection(client, "https://api.groq.com/openai/v1/chat/completions")
    assert seen["method"] == "GET" and "api.groq.com" in seen["url"]
    await client.aclose()


async def test_warm_connection_swallows_unreachable_host():
    client = make_async_client(timeout_s=1.0)
    # Nothing listening → connect error; must be swallowed, not raised.
    await warm_connection(client, "http://127.0.0.1:1/anything", timeout_s=0.3)
    await client.aclose()


# --- provider.warm() ----------------------------------------------------------

async def test_groq_provider_warm_hits_its_endpoint_host():
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["method"], seen["url"] = req.method, str(req.url)
        return httpx.Response(200, json={"data": []})

    p = GroqNlpProvider(api_key="k", model="m")
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await p.warm()
    assert seen["method"] == "GET" and "chat/completions" in seen["url"]
    await p.aclose()


async def test_groq_provider_warm_swallows_failure():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("upstream down")

    p = GroqNlpProvider(api_key="k", model="m")
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await p.warm()  # must not raise
    await p.aclose()


async def test_stub_provider_warm_is_noop():
    await StubNlpProvider().warm()  # default ABC no-op — no client, no raise


# --- ServiceClients.warm_up() fan-out ----------------------------------------

async def test_warm_up_pings_all_three_services_and_swallows_errors():
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        raise httpx.ConnectError("service warming path down")  # still must not raise

    c = ServiceClients(CommonSettings())
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await c.warm_up()  # best-effort: no exception despite every ping failing
    warmups = [p for p in calls if p.endswith("/v1/warmup")]
    assert len(warmups) == 3  # stt + nlp + tts
    await c.aclose()
