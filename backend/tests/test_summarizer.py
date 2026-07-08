"""Post-call summarizer: Sarvam chat completions -> structured CallSummary."""

from __future__ import annotations

import httpx
import pytest

from koel.common.schemas import Language
from koel.voicebot.pipeline.summarizer import SarvamSummarizer, transcript_text
from koel.voicebot.session.models import Turn


def _turns() -> list[Turn]:
    return [
        Turn(
            index=0, speaker="bot", text="Namaste, interested in a Term Life Plan?",
            language=Language.hinglish,
        ),
        Turn(index=1, speaker="customer", text="Yes tell me more", language=Language.hinglish),
    ]


def test_transcript_text_joins_speaker_and_text():
    text = transcript_text(_turns())
    assert text == "BOT: Namaste, interested in a Term Life Plan?\nCUSTOMER: Yes tell me more"


async def test_summarize_parses_plain_json(monkeypatch):
    s = SarvamSummarizer(api_key="k")
    captured: dict = {}

    async def ok(url, headers=None, json=None):  # noqa: ANN001
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        content = (
            '{"interest_shown": true, "objection": "", '
            '"sentiment": "positive", "next_action": "Follow up"}'
        )
        payload = {"choices": [{"message": {"content": content}}]}
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    monkeypatch.setattr(s._client, "post", ok)
    result = await s.summarize(_turns())

    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["headers"]["api-subscription-key"] == "k"
    assert captured["json"]["model"] == "sarvam-30b"
    assert result.interest_shown is True
    assert result.sentiment == "positive"
    assert result.next_action == "Follow up"
    await s.aclose()


async def test_summarize_strips_markdown_json_fence(monkeypatch):
    s = SarvamSummarizer(api_key="k")

    async def ok(url, headers=None, json=None):  # noqa: ANN001
        content = (
            "```json\n"
            '{"interest_shown": false, "objection": "too expensive", '
            '"sentiment": "negative", "next_action": "close"}\n'
            "```"
        )
        payload = {"choices": [{"message": {"content": content}}]}
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    monkeypatch.setattr(s._client, "post", ok)
    result = await s.summarize(_turns())
    assert result.interest_shown is False
    assert result.objection == "too expensive"
    await s.aclose()


async def test_summarize_raises_on_unreachable_host():
    s = SarvamSummarizer(api_key="k", base_url="http://127.0.0.1:1", timeout_s=2)
    with pytest.raises(httpx.HTTPError):
        await s.summarize(_turns())
    await s.aclose()
