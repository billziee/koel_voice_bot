"""Post-call summarizer: Sarvam chat completions extracts structured insight
(interest/objection/sentiment/next action) from a call transcript so it can be
written back onto the customer's CRM record. Off the in-call critical path —
this only ever runs after a call has ended (see ws/handler.py _finalize)."""

from __future__ import annotations

import json
import re

from pydantic import BaseModel

from koel.common.http import make_async_client
from koel.voicebot.session.models import Turn

_SYSTEM_PROMPT = (
    "You are a call-analytics assistant for a sales/collections voicebot. Given a "
    "call transcript, answer with a compact JSON object with exactly these keys: "
    "interest_shown (boolean), objection (string, empty if none), sentiment (one of "
    "positive/neutral/negative), next_action (short string). Output ONLY the JSON "
    "object, nothing else."
)

_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class CallSummary(BaseModel):
    interest_shown: bool
    objection: str = ""
    sentiment: str = "neutral"
    next_action: str = ""


def transcript_text(turns: list[Turn]) -> str:
    return "\n".join(f"{t.speaker.upper()}: {t.text}" for t in turns)


class SarvamSummarizer:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "sarvam-30b",
        base_url: str = "https://api.sarvam.ai",
        timeout_s: float = 40.0,
    ) -> None:
        self._model = model
        self._url = f"{base_url.rstrip('/')}/v1/chat/completions"
        self._headers = {"api-subscription-key": api_key, "Content-Type": "application/json"}
        self._client = make_async_client(timeout_s=timeout_s)

    async def summarize(self, turns: list[Turn]) -> CallSummary:
        text = transcript_text(turns)
        resp = await self._client.post(
            self._url,
            headers=self._headers,
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
            },
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
        fenced = _JSON_FENCE.search(content)
        payload = json.loads(fenced.group(1) if fenced else content)
        return CallSummary.model_validate(payload)

    async def aclose(self) -> None:
        await self._client.aclose()
