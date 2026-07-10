# 9 June — Autonomous Decision Log

Context: the operator granted full autonomy ("leaving all decision making to you"),
explicitly opted into multi-agent orchestration ("use subagents and workflows"), and
asked to maximise throughput on a 100%-available team-plan budget. This file records
every non-trivial decision made during that autonomous block. (System clock reads
2026-07-09; filename uses the operator's "9 june" label.)

## Standing decisions

1. **Reverse the earlier "no subagents / main-window-only" constraint.** The operator
   directly and explicitly instructed the opposite this session, so parallel subagents
   and `Workflow` orchestration are now in use. A later instruction overrides an earlier
   standing one.
2. **Every code change ships as its own PR, verified before merge.** Verify gates:
   backend → `ruff` + `pytest --cov` (≥85% gate; ~90% actual); frontend → `tsc --noEmit`
   + `npm run build` (+ Playwright cross-browser, via CI when local runs would collide).
3. **Merge policy: the orchestrator (main agent) reviews + merges.** Implementer subagents
   open PRs but do **not** self-merge. The critical-path change (O-2 activation) additionally
   gets adversarial multi-lens review before merge.
4. **Commit style matches the repo:** conventional commits, no attribution footer (matches
   PRs #20–#33 this session).

## Parallelism design

- Independent items run as **worktree-isolated** parallel agents (each on its own branch
  from `main`, touching non-overlapping files), so concurrent file edits can't collide.
- **Port constraint:** Playwright's dev server binds `:5173`, so two frontend e2e runs
  can't run concurrently. Decision: frontend agents verify with `tsc` + `npm run build`
  locally and rely on **CI** for the cross-browser Playwright run (CI is the merge gate
  regardless). Backend agents run full `pytest` locally (in-process SQLite, no shared port).

## Work items dispatched (this block)

| Item | What | Risk | Verify |
|------|------|------|--------|
| **A. O-2 activation** | Wire the orchestrator turn loop to stream NLP sentences → TTS behind `stream_nlp` (the −200–500 ms first-audio win); batch path stays the fallback | High (live-call critical path) | pytest + adversarial review |
| **B. Demo-button overlap** | Fix the sticky bottom bar overlapping "Run Guided Demo" at 1280×720; revert the e2e `dispatchEvent` workaround to a normal click | Low | tsc+build local, e2e via CI |
| **C. Q-7 e2e expansion** | Add deterministic critical-flow tests (config/history/nav/dark-mode/reload persistence) toward the plan's ~15–20 target | Low | tsc+build local, e2e via CI |
| **D. O-10 DB pool** | Add SQLAlchemy pool-checkout-wait observability + a configurable pgvector `hnsw.ef_search` knob + right-sized defaults with rationale (load-test itself is Phase-2/out of scope) | Low–med | pytest |

### O-2 activation design (item A)

- New `Orchestrator._stream_bot_turn(state, send, ctx) -> NlpResponse`: one bot turn, one
  transcript entry, one `bot_speaking` state; iterates `clients.respond_stream(ctx)`,
  synthesizes **each sentence as it arrives** (reusing the sentence-streaming TTS path),
  captures the terminal `final` frame's full `NlpResponse` for control flow.
- `_handle_turn` gains a `stream_nlp and stream_tts` branch that calls `_stream_bot_turn`
  and then applies the **same** intent/escalate/end_call/budget control flow **without
  re-speaking** `response_text` (it's already streamed). Batch path unchanged when the flag
  is off.
- **First cut skips the separate filler race in stream mode** (streaming already minimises
  first-audio latency); combining filler + streaming is noted as a follow-up.
- Fail-safe preserved: `respond_stream` / the provider degrade to batch on any stream error
  (built in #31–#33), so a live turn is never dropped.

## Deferred (blocked, not skipped)

- **O-5 / O-6** (speculative RAG ∥ STT, streaming STT partials) — build on O-2 streaming;
  sequence after O-2 is active and profiled.
- **O-7** (in-region inference) — gated on the residency/legal ruling.
- **Part C Themes 1–6** — product features; need a planning pass, not an autonomous PR.
- **Q-7 dynamic edge sims** (barge-in / escalation / resume) — timing-gated, flake-prone
  headless; only deterministic flows added.

## Outcomes

All landed on `main`, each CI-green + locally verified before merge. Final integration
check on the composed `main`: backend **334 passed, 90.6% coverage, ruff clean**; frontend
**tsc + build clean**.

| PR | What | Verify |
|----|------|--------|
| #34 | This decision log | docs |
| #35 | Q-7 deterministic nav/dark-mode/reload e2e (suite ~11 → ~17) | 18/18 local × 3 browsers + CI |
| #36 | Demo-button overlap fix; e2e reverted to a normal click | 9/9 call-flow × 3 browsers + CI |
| #37 | **O-2 activation** — orchestrator streams NLP sentences → TTS behind `stream_nlp` | adversarial review + 7 turn tests + CI |
| #38 | O-10 DB pool checkout observability + pgvector `ef_search` knob | full suite + CI |
| #39 | Guard `FastEmbedEmbedder` load so a bad model can't crash nlp_svc | full suite + CI |
| #40 | Rate-limiter per-route namespace + fail-closed login throttle | full suite + CI |
| #41 | Bound TTS-cache PII retention (configurable, 3h → 30 min default) | full suite + CI |

**Multi-agent execution:** two `Workflow` runs of worktree-isolated parallel agents produced
#35/#36/#38 and #39/#40/#41; a security-reviewer agent swept the session's recent backend
surface (NLP streaming, rate limiter, embedder, TTS cache) and a second adversarial reviewer
scrutinised the O-2 turn loop. Both reviews found real defects that were fixed before merge:

- **O-2 turn-loop review (all fixed in #37):** a lost terminal frame after partial audio no
  longer silently drops `opt_out`/`escalate` (reconciles via batch `respond()`, else escalates
  to a human — TRAI §11); an empty stream falls back to batch (no silent turn); the persisted
  Turn/transcript carry the resolved tone/intent; a cancelled turn is still recorded for the
  audit trail; the route always emits a terminal frame; the sentence streamer fails safe on a
  UTF-16 surrogate escape.
- **Backend security sweep (fixed in #39/#40/#41):** fastembed startup crash; rate-limiter
  cross-endpoint quota contamination + login fail-open; TTS-cache PII retention window.

**O-2 status:** fully activated end-to-end behind `KOEL_STREAM_NLP` (default off). Enabling it
on VB_Svc turns on token-bound first-audio; the batch path remains the fallback.

**Follow-ups noted (not done this block):** combine the filler race with streaming; apply the
sentiment-adjusted tone to streamed prosody; O-5/O-6 (build on O-2, sequence after profiling);
O-7 (legal-gated); O-10 load-test harness (Phase 2); Part C product themes (need planning).
