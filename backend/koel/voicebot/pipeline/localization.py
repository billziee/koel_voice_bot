"""Single-canonical-source localization for hi/hinglish operational copy.

Historically every bot utterance needing both a Hindi (Magpie, Devanagari-only)
and Hinglish (Orpheus, romanized) form was hand-authored twice — that drift is
exactly what caused the dc656ba/09adce6 script-leak bugs (romanized text
reaching Magpie, producing gibberish mid-call). Devanagari stays authored by
hand; the Hinglish form is derived from it via Sarvam transliteration at
startup (``warm()``), not hand-maintained.

A hardcoded Hinglish fallback ships with every entry so a Sarvam outage at
boot never blocks startup or leaves a language without copy — ``warm()``
degrades to keeping that fallback in place, exactly like every other Sarvam
integration in this codebase (TTS/STT cascade to a fallback, never raise).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, ItemsView
from typing import TypeVar

from koel.common.schemas import Language

logger = logging.getLogger(__name__)

Transliterate = Callable[[str], Awaitable[str]]


class Localized:
    """A Language -> str table. ``hi`` is authored; ``hinglish`` starts as a
    hardcoded fallback and is replaced by a live transliteration on ``warm()``."""

    def __init__(self, *, hi: str, hinglish_fallback: str, en: str | None = None) -> None:
        self._table: dict[Language, str] = {Language.hi: hi, Language.hinglish: hinglish_fallback}
        if en is not None:
            self._table[Language.en] = en

    def __getitem__(self, language: Language) -> str:
        return self._table[language]

    def get(self, language: Language, default: Language = Language.hinglish) -> str:
        return self._table.get(language, self._table[default])

    def __contains__(self, language: Language) -> bool:
        return language in self._table

    @property
    def hi(self) -> str:
        return self._table[Language.hi]

    def _set_hinglish(self, value: str) -> None:
        self._table[Language.hinglish] = value


class LocalizedList:
    """Same idea as ``Localized`` for a rotation of phrases (e.g. fillers)."""

    def __init__(self, *, hi: list[str], hinglish_fallback: list[str], en: list[str] | None = None) -> None:
        self._table: dict[Language, list[str]] = {Language.hi: hi, Language.hinglish: hinglish_fallback}
        if en is not None:
            self._table[Language.en] = en

    def __getitem__(self, language: Language) -> list[str]:
        return self._table[language]

    def get(self, language: Language, default: Language = Language.en) -> list[str]:
        return self._table.get(language, self._table[default])

    def items(self) -> ItemsView[Language, list[str]]:
        return self._table.items()

    @property
    def hi(self) -> list[str]:
        return self._table[Language.hi]

    def _set_hinglish(self, value: list[str]) -> None:
        self._table[Language.hinglish] = value


_REGISTRY: list[Localized | LocalizedList] = []

# Constrained TypeVar so register() is identity-typed: register(Localized(...))
# returns Localized and register(LocalizedList(...)) returns LocalizedList, rather
# than widening every module-level table to the union (which forced callers like
# orchestrator._by_language and filler.FILLER_PHRASES.items() through a union that
# mypy couldn't resolve).
_Entry = TypeVar("_Entry", "Localized", "LocalizedList")


def register(entry: _Entry) -> _Entry:
    """Register a table so ``warm()`` can refresh its Hinglish entry."""
    _REGISTRY.append(entry)
    return entry


async def warm(transliterate: Transliterate) -> None:
    """Best-effort: replace each registered table's Hinglish fallback with a
    live transliteration of its Hindi source. Never raises — a failure just
    means that entry keeps its hardcoded fallback."""
    for entry in _REGISTRY:
        try:
            if isinstance(entry, Localized):
                entry._set_hinglish(await transliterate(entry.hi))  # noqa: SLF001
            else:
                entry._set_hinglish([await transliterate(p) for p in entry.hi])  # noqa: SLF001
        except Exception:  # noqa: BLE001 — keep the fallback, never block boot
            logger.warning("localization_warm_failed", exc_info=True)
