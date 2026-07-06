"""_build_provider() wiring: Sarvam/Magpie/base cascade + the hi-only routing
restriction that applies when Sarvam is disabled (Magpie can't take hinglish)."""

from __future__ import annotations

import pytest

from koel.tts.config import get_tts_settings
from koel.tts.providers import NvidiaMagpieTtsProvider, RoutingTtsProvider, SarvamTtsProvider


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    # get_tts_settings is @lru_cache'd — clear before AND after so this file's
    # monkeypatched env vars never leak into (or out of) other test modules.
    get_tts_settings.cache_clear()
    yield
    get_tts_settings.cache_clear()


def _rebuild_provider(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_tts_settings.cache_clear()
    from koel.tts.main import _build_provider

    return _build_provider()


async def test_sarvam_and_magpie_both_enabled_routes_hi_and_hinglish(monkeypatch):
    provider = _rebuild_provider(
        monkeypatch,
        KOEL_SARVAM_API_KEY="sk_test",
        KOEL_TTS_HINDI_PROVIDER="nvidia",
        KOEL_NVIDIA_API_KEY="nvapi-test",
    )
    assert isinstance(provider, RoutingTtsProvider)
    assert provider._langs == {"hi", "hinglish"}  # noqa: SLF001
    assert isinstance(provider._hindi, SarvamTtsProvider)  # noqa: SLF001
    assert isinstance(provider._hindi._hi_fallback, NvidiaMagpieTtsProvider)  # noqa: SLF001


async def test_magpie_only_restricts_routing_to_hi(monkeypatch):
    # Without Sarvam, Magpie can't safely take hinglish (Devanagari-only) —
    # the routed set must be narrowed even if config says otherwise.
    provider = _rebuild_provider(
        monkeypatch,
        KOEL_TTS_HINDI_PROVIDER="nvidia",
        KOEL_NVIDIA_API_KEY="nvapi-test",
        KOEL_TTS_NVIDIA_LANGUAGES="hi,hinglish",
    )
    assert isinstance(provider, RoutingTtsProvider)
    assert provider._langs == {"hi"}  # noqa: SLF001
    assert isinstance(provider._hindi, NvidiaMagpieTtsProvider)  # noqa: SLF001


async def test_neither_enabled_returns_base_directly(monkeypatch):
    # The local dev .env defaults KOEL_TTS_HINDI_PROVIDER=nvidia — override it
    # explicitly so this test genuinely exercises "neither configured".
    provider = _rebuild_provider(monkeypatch, KOEL_TTS_HINDI_PROVIDER="default")
    assert not isinstance(provider, RoutingTtsProvider)
