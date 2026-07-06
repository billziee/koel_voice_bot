"""Single-canonical-source localization: Devanagari authored, Hinglish derived
via Sarvam transliteration at startup with a hardcoded fallback (PRD §06)."""

from __future__ import annotations

import pytest

from koel.common.schemas import Language
from koel.voicebot.pipeline import localization


@pytest.fixture
def isolated_registry():
    """conftest.py imports filler/orchestrator at collection time, which
    registers real entries — clear them out so warm() only ever processes
    what a given test explicitly registers."""
    snapshot = list(localization._REGISTRY)  # noqa: SLF001
    localization._REGISTRY.clear()  # noqa: SLF001
    yield
    localization._REGISTRY[:] = snapshot  # noqa: SLF001


def test_localized_falls_back_before_warm():
    entry = localization.Localized(hi="नमस्ते", hinglish_fallback="Namaste")
    assert entry[Language.hi] == "नमस्ते"
    assert entry[Language.hinglish] == "Namaste"
    assert entry.get(Language.en) == "Namaste"  # default is hinglish, no en entry


def test_localized_list_falls_back_before_warm():
    entry = localization.LocalizedList(hi=["एक", "दो"], hinglish_fallback=["Ek", "Do"])
    assert entry[Language.hi] == ["एक", "दो"]
    assert entry[Language.hinglish] == ["Ek", "Do"]


async def test_warm_replaces_hinglish_with_transliteration(isolated_registry):
    entry = localization.register(localization.Localized(hi="नमस्ते", hinglish_fallback="STALE"))

    async def fake_transliterate(text: str) -> str:
        assert text == "नमस्ते"
        return "Namaste (live)"

    await localization.warm(fake_transliterate)
    assert entry[Language.hinglish] == "Namaste (live)"


async def test_warm_keeps_fallback_on_transliteration_failure(isolated_registry):
    entry = localization.register(localization.Localized(hi="नमस्ते", hinglish_fallback="Namaste"))

    async def failing_transliterate(text: str) -> str:
        raise RuntimeError("Sarvam unreachable")

    await localization.warm(failing_transliterate)  # must not raise
    assert entry[Language.hinglish] == "Namaste"  # fallback untouched


async def test_warm_handles_a_localized_list_too(isolated_registry):
    entry = localization.register(
        localization.LocalizedList(hi=["एक", "दो"], hinglish_fallback=["STALE1", "STALE2"])
    )
    counter = iter(["Ek (live)", "Do (live)"])

    async def fake_transliterate(text: str) -> str:
        return next(counter)

    await localization.warm(fake_transliterate)
    assert entry[Language.hinglish] == ["Ek (live)", "Do (live)"]


def test_existing_operational_strings_are_registered():
    # filler/silence/orchestrator each register their tables at import time.
    from koel.voicebot.pipeline import filler, orchestrator, silence  # noqa: F401

    assert len(localization._REGISTRY) >= 6  # noqa: SLF001 — filler + 2 silence + 3 orchestrator
