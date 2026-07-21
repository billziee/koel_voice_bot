"""Speculative RAG (O-5) divergence check.

O-5 fires RAG retrieval EARLY — during the endpointing hold window, on the held
mid-thought fragment — so the chunks are already in hand when the final turn
runs. That is only safe to reuse if the final transcript is still asking the
same thing as the speculative query; otherwise the pre-fetched chunks are stale
and NLP_svc must retrieve for the real query instead.

This decides that. It is PURE and deterministic (no I/O, no clock) so it
unit-tests in isolation and adds nothing to the turn hot path but a set compare.
"""

from __future__ import annotations

import asyncio
import re

from koel.common.logging import get_logger
from koel.nlp.schemas import RetrievedChunk

logger = get_logger("koel.voicebot.specrag")

_WS = re.compile(r"\s+")

# Token-overlap (Jaccard) at or above this ⇒ the two queries overlap enough to
# reuse the speculative chunks. Below it ⇒ diverged, re-retrieve. 0.6 tolerates a
# short follow-up appended to the held fragment (and minor STT wording drift)
# while still catching a genuinely different question.
_JACCARD_REUSE_THRESHOLD = 0.6


def _normalize(text: str) -> str:
    """Lowercase, strip, and collapse internal whitespace runs to one space."""
    return _WS.sub(" ", text.strip().lower())


def transcripts_diverged(speculative: str, final: str) -> bool:
    """Whether ``final`` diverged from the ``speculative`` query enough that the
    pre-fetched chunks must NOT be reused.

    Returns ``False`` (reuse the chunks) when, after normalising (lowercase,
    strip, collapse whitespace):
      - the final is a prefix/continuation of the speculative query, or vice
        versa (the merge path appends the follow-up to the held fragment, so the
        final normally *starts with* the speculative text), or
      - their token sets overlap by Jaccard >= 0.6.

    Returns ``True`` (discard + re-retrieve) otherwise. An empty speculative
    query is always divergent (there is nothing to reuse), as is an empty final.
    """
    spec = _normalize(speculative)
    fin = _normalize(final)
    if not spec or not fin:
        return True
    # Prefix / continuation in either direction ⇒ same thought continuing.
    if spec.startswith(fin) or fin.startswith(spec):
        return False
    spec_tokens = set(spec.split())
    fin_tokens = set(fin.split())
    union = spec_tokens | fin_tokens
    if not union:
        return True
    jaccard = len(spec_tokens & fin_tokens) / len(union)
    return jaccard < _JACCARD_REUSE_THRESHOLD


class SpeculativeRagCoordinator:
    """Owns the O-5 in-flight speculative-RAG prefetch state for one call —
    fire during a smart-turn hold, resolve (or discard, on divergence/timeout)
    when the held turn finally runs, cancel on teardown/barge-in. Framed as a
    standalone unit (rather than inline on a transport's call-session class) so
    a second transport can reuse the identical fire/resolve/cancel logic
    instead of re-implementing it; ``ws/handler.CallSession`` predates this and
    keeps its own inline version.
    """

    def __init__(self, *, settings, state, clients, orchestrator) -> None:  # noqa: ANN001
        self._settings = settings
        self._state = state
        self._clients = clients
        self._orch = orchestrator
        self._inflight: tuple[str, asyncio.Task[list[RetrievedChunk]]] | None = None

    def start(self, held_text: str) -> None:
        """Fire retrieval for the held mid-thought text in the background.
        Best-effort and fully guarded — the kill-switch off, a RAG-disabled
        bot, a client without the O-5 method, or blank text all just skip
        speculation (the turn falls back to NLP-side retrieval). A stale
        in-flight pre-fetch from an earlier hold is cancelled first."""
        self.cancel()
        if not self._settings.speculative_rag_enabled:
            return
        if not (self._state.rag_enabled and self._state.rag_index) or not held_text.strip():
            return
        retrieve = getattr(self._clients, "retrieve", None)
        if retrieve is None:  # a client build without the O-5 method — skip
            return
        try:
            ctx = self._orch.build_speculative_context(self._state, held_text)
        except Exception:  # noqa: BLE001 — context build must never break the hold
            logger.debug("speculative_context_build_failed", exc_info=True)
            return
        task = asyncio.create_task(self._run(retrieve, ctx))
        self._inflight = (held_text, task)

    async def _run(self, retrieve, ctx) -> list[RetrievedChunk]:  # noqa: ANN001
        try:
            return await retrieve(ctx)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — best-effort pre-fetch
            logger.debug("speculative_retrieval_failed", exc_info=True)
            return []

    def cancel(self) -> None:
        """Drop + cancel any in-flight speculative pre-fetch (stale hold,
        barge-in, call-end)."""
        if self._inflight is not None:
            _, task = self._inflight
            self._inflight = None
            task.cancel()

    async def resolve(self, final_text: str) -> list[RetrievedChunk] | None:
        """Turn-time decision: reuse the in-flight speculative chunks when the
        final transcript hasn't diverged from the speculative query, else
        discard and let NLP_svc retrieve normally. Returns None for no
        speculation / diverged / timeout / error / empty. Never blocks the
        turn beyond the configured await bound and never raises
        (CancelledError still propagates for barge-in / shutdown)."""
        spec = self._inflight
        self._inflight = None
        if spec is None:
            return None
        spec_text, task = spec
        if transcripts_diverged(spec_text, final_text):
            task.cancel()
            return None
        try:
            chunks = await asyncio.wait_for(
                asyncio.shield(task),
                timeout=self._settings.spec_retrieval_await_ms / 1000,
            )
        except Exception:  # noqa: BLE001 — timeout/error ⇒ NLP retrieves instead
            task.cancel()
            return None
        return chunks or None
