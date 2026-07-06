"""Sarvam transliteration client — hi-IN (Devanagari) -> en-IN (romanized).

Used only at startup (see localization.warm()) to derive Hinglish operational
copy from its Devanagari source. Never called on the live-call path, so a
Sarvam outage here only affects boot-time copy generation, not call latency.
"""

from __future__ import annotations

from koel.common.http import make_async_client


class SarvamTransliterator:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.sarvam.ai",
        timeout_s: float = 15.0,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/transliterate"
        self._headers = {"api-subscription-key": api_key, "Content-Type": "application/json"}
        self._client = make_async_client(timeout_s=timeout_s)

    async def to_hinglish(self, text: str) -> str:
        resp = await self._client.post(
            self._url,
            headers=self._headers,
            json={"input": text, "source_language_code": "hi-IN", "target_language_code": "en-IN"},
        )
        resp.raise_for_status()
        result = (resp.json().get("transliterated_text") or "").strip()
        if not result:
            raise ValueError("Sarvam transliterate returned empty text")
        return result

    async def aclose(self) -> None:
        await self._client.aclose()
