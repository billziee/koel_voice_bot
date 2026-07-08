"""Per-bot LLM / prompt overrides (roadmap 5.4).

The GroqNlpProvider honours per-turn model / temperature / prompt-preamble
overrides (falling back to the routed model + service defaults), and the bots
admin API round-trips + audits the new columns.
"""

from __future__ import annotations

import json

import httpx
from sqlalchemy import select

from koel.common.auth import issue_token
from koel.common.schemas import Language
from koel.db.models import BotConfig, BotConfigAudit
from koel.nlp.providers import GroqNlpProvider
from koel.nlp.schemas import TurnContext


def _headers(org_id: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token('op', kind='access', org_id=org_id)}"}


def _groq() -> GroqNlpProvider:
    return GroqNlpProvider(
        api_key="k", model="base", model_fast="fast-8b",
        model_quality="quality-70b", temperature=0.3,
    )


def _capture(captured: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        content = json.dumps({"response_text": "ok", "intent": "interested", "tone": "cheerful"})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 5}}
        )

    return handler


def _ctx(**over) -> TurnContext:
    base = dict(
        session_id="s", bot_id="b", use_case="credit_card",
        language=Language.hinglish, customer_text="haan",
    )
    base.update(over)
    return TurnContext(**base)


# --- provider honours overrides ----------------------------------------------

async def test_generate_applies_per_bot_overrides():
    captured: dict = {}
    p = _groq()
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(_capture(captured)))
    await p.generate(
        _ctx(llm_model="bot-model", llm_temperature=0.9, prompt_preamble="Be extra warm."), []
    )
    payload = captured["payload"]
    assert payload["model"] == "bot-model"  # override beats the routed default
    assert payload["temperature"] == 0.9
    system = payload["messages"][0]["content"]
    assert "You are KOEL" in system  # hardened prompt is retained…
    assert "Be extra warm." in system  # …and the preamble is appended, not swapped in
    await p.aclose()


async def test_generate_falls_back_to_routed_defaults():
    captured: dict = {}
    p = _groq()
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(_capture(captured)))
    await p.generate(_ctx(), [])  # hinglish, no overrides
    payload = captured["payload"]
    assert payload["model"] == "quality-70b"  # language-routed default (70b for hinglish)
    assert payload["temperature"] == 0.3  # service default
    assert "ADDITIONAL BOT-SPECIFIC GUIDANCE" not in payload["messages"][0]["content"]
    await p.aclose()


# --- bots admin API round-trips the new columns ------------------------------

async def test_create_and_patch_roundtrip_overrides(app_client, test_db):
    client, _ = app_client
    r = await client.post("/v1/bots", headers=_headers(org_id="acme"), json={
        "name": "Tuned Bot", "use_case": "collections",
        "llm_model": "llama-3.1-8b-instant", "llm_temperature": 0.2,
        "prompt_preamble": "Speak briefly and firmly.",
    })
    assert r.status_code == 201, r.text
    bot_id = r.json()["id"]
    assert r.json()["llm_model"] == "llama-3.1-8b-instant"
    assert r.json()["llm_temperature"] == 0.2
    assert r.json()["prompt_preamble"] == "Speak briefly and firmly."

    # PATCH the temperature; the change is audited (the new column is in _AUDITED).
    p = await client.patch(
        f"/v1/bots/{bot_id}", headers=_headers(org_id="acme"), json={"llm_temperature": 0.7}
    )
    assert p.status_code == 200 and p.json()["llm_temperature"] == 0.7

    async with test_db() as s:
        row = (await s.execute(select(BotConfig).where(BotConfig.id == bot_id))).scalar_one()
        assert row.llm_model == "llama-3.1-8b-instant" and row.llm_temperature == 0.7
        audits = (
            await s.execute(select(BotConfigAudit).where(BotConfigAudit.bot_id == bot_id))
        ).scalars().all()
    upd = next(a for a in audits if a.op == "update")
    assert upd.before["llm_temperature"] == 0.2 and upd.after["llm_temperature"] == 0.7


async def test_llm_temperature_out_of_range_rejected(app_client):
    client, _ = app_client
    r = await client.post("/v1/bots", headers=_headers(org_id="acme"), json={
        "name": "Bad", "use_case": "credit_card", "llm_temperature": 3.0,
    })
    assert r.status_code == 422  # ge=0.0, le=2.0
