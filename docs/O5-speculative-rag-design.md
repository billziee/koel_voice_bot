# O-5 — Speculative / Parallel Pipeline: design + blocker

**Status: BLOCKED on O-6 (streaming STT ingest) + profiling. Not implemented.**

This note records why O-5 can't be completed independently, the concrete design for when
its prerequisite lands, and the guardrails it needs — so it's ready to execute rather than
re-derived later.

## What O-5 is

Hide RAG-retrieval latency (embed query + pgvector ANN search, ~10–250 ms depending on the
embedder) by overlapping it with work already on the critical path, instead of running it
strictly serially before generation. Plan gain: −50–250 ms/turn, shrinking to −30–80 ms after
O-4's local embedder removes the cross-ocean hop.

## Why it's blocked right now

The current turn is serial by construction and has **nothing to overlap retrieval with**:

1. VB_Svc buffers the caller's audio and does **one batch** `POST /v1/transcribe`
   (`ws/handler.py:265`) — there is no transcript until STT fully finishes.
2. VB_Svc then calls NLP_svc `respond()` / `respond/stream()`, which does
   `retrieved = await _retrieve_for(ctx)` **then** generates (`nlp/main.py`). Generation needs
   the retrieved chunks in the prompt, so retrieval and generation *cannot* overlap within NLP.

So the only place to hide retrieval is **before** the final transcript exists — i.e. speculate
on a *partial* transcript. That partial only exists once **O-6 streaming STT** forwards frames
and emits partials. Without O-6, there is no earlier signal to kick retrieval off from. The
plan's own dependency line says as much ("partials feed O-5's speculative retrieval"); the
endpointing signal (smart-turn, merged) tells us *when* the turn is ending but not *what* was
said early enough to prefetch.

Additionally, the plan gates O-5 on **profiling**: "only if profiling still shows retrieval on
the critical path" — and after O-4 (local CPU embedder, ~10–40 ms) retrieval may no longer be
worth speculating on. That measurement needs the O-10 load harness against a real deployment.

## Design (execute once O-6's ingest is live + profiling justifies it)

Prereqs: O-6 wired into `ws/handler.py` (partial `TranscriptResult`s available mid-utterance);
a profiling run (O-10 harness) confirming retrieval is still on the critical path post-O-4.

1. **NLP_svc: split retrieval from generation.** Add `POST /v1/retrieve` (reuses the existing
   `_retrieve_for(ctx)` — already extracted) returning `list[RetrievedChunk]`, and accept
   pre-fetched chunks on `respond()`/`respond/stream()` (an optional `prefetched: list[...] |
   None` on `TurnContext`; when present, skip the internal retrieve).
2. **VB_Svc: speculative prefetch.** On the first *stable* partial transcript (a confidence /
   token-count threshold, or the smart-turn "likely ending" signal), fire
   `clients.retrieve(partial_ctx)` as a background task in parallel with the remaining
   STT finalization. Behind a `speculative_rag` flag (default off).
3. **Reconcile at final.** When the final transcript lands, if it's ~unchanged from the
   speculated partial (normalized-equality / high overlap), pass the prefetched chunks into
   `respond()` and skip re-retrieval (the win). If it diverged, discard and retrieve normally
   (correctness over speed).

## Guardrails (non-negotiable)

- **Never speak on a speculated turn that the final transcript contradicts** — prefetch only
  the *retrieval*, never the generation/audio; generation still waits for the final transcript.
- **Bounded waste:** cancel the in-flight prefetch if the final transcript diverges; cap one
  speculative retrieve per turn.
- **Flag-gated + fallback:** `speculative_rag` off → exact current behaviour; any prefetch
  error → normal serial retrieve (RAG is already degrade-safe).
- **Cost:** a discarded speculative retrieve is one wasted embed + ANN query; measure the
  hit-rate (speculated == final) before defaulting it on — if low, the extra DB/embed load
  isn't worth the latency shaved.

## Recommendation

Do **not** implement O-5 until: (a) O-6's streaming ingest is live and verified against real
Sarvam WS STT audio, and (b) the O-10 harness shows retrieval is still materially on the
critical path after O-4. Until then this is speculative complexity on the live turn loop with
no measurable, verifiable win — so it's documented, not coded.
