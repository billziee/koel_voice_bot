# KOEL — Service Level Objectives & Alerting v4

> Supersedes [SLO_V3.md](./SLO_V3.md). v4 covers the telephony/web parity cycle (31 commits, headlined by full `TelephonyCallSession` parity). No targets moved and no new metric instruments were added — the only SLO-relevant change is that telephony calls now emit the same first-audio and turn latency signal the browser transport always has, closing a metrics blind spot rather than shifting a number. Verified directly against `koel/common/telemetry.py` (byte-identical to the v3 commit) and `koel/voicebot/telephony/session.py`.

## Objectives (v1 → v2 → v3 → v4)

| SLI | v1 target | v2 measured | v3 | v4 |
|---|---|---|---|---|
| First-audio p50 | <300ms (turn) | ~1.0s (perceived <300ms via filler) | token-bound (O-2 NLP stream + O-6 STT partials) → toward ~0.5–0.7s true (US inference) | unchanged target; telephony calls now contribute to the same `koel.first_audio.latency` histogram (previously browser-only — see below) |
| First-audio p95 | — | ~2.1–2.3s | shrinks with streaming; filler still masks tail | unchanged; not re-measured this cycle (feature-parity cycle, not a perf cycle) |
| Turn error rate | — | 0% @ 10 concurrent | preserved; every fast path degrades to the proven batch path | unchanged; bug-hunt batches 1–3b (commits `dd78305`/`7845574`/`a20d3d1`/`2dc262c`) fixed correctness/data-loss bugs, not error-rate targets |
| Concurrency correctness | design | single-worker | >1 worker correct (O-8 Redis limiter) + observable (O-10 pool) | unchanged |
| Per-call cost | — | — | estimated per turn; hard per-session ceiling (O-9) | unchanged; no new cost-relevant paths (outbound dialer reuses the same per-turn NLP cost accounting) |

## Metrics (v4: no new instruments)

`koel/common/telemetry.py` is unchanged since v3 — same five histograms/counters (`koel.turn.latency`, `koel.first_audio.latency`, `koel.hop.latency`, `koel.errors`, `koel.nlp.tokens`/`koel.nlp.cost_usd`) and the two O-10 pool gauges. `TelephonyCallSession` (new in this cycle, `koel/voicebot/telephony/session.py`) calls `record_first_audio_latency` and relies on `Orchestrator.handle_final_transcript` for `record_turn_latency` — both are the *same* instruments v3 already lists, not new ones.

What actually changed: at v3, telephony's bridge called `orchestrator.handle_final_transcript` directly, so full turn latency (`koel.turn.latency`, audio-in→audio-out complete) was already transport-agnostic and already covered telephony. But nothing on the telephony side ever marked the *first* bot-audio-out instant, so `koel.first_audio.latency` — the perceived-responsiveness metric the browser transport has driven since the streaming-latency work — was browser-only data. `TelephonyCallSession.on_outbound` now marks `_turn_in_at` on each customer utterance and records `record_first_audio_latency` on the first outbound `audio_chunk`, mirroring `ws/handler.py` exactly. Net effect: the first-audio p50/p95 numbers above will, going forward, blend both transports instead of silently excluding phone calls — worth knowing when reading the dashboard, not a target change.

## New operational notes (not new SLIs)

- **No-input watchdog granularity**: telephony's no-input clock (`_no_input_watchdog` in `session.py`) polls every `_NO_INPUT_POLL_INTERVAL_S` = 1s, versus the browser's precise single-shot `asyncio.wait_for(receive_text(), timeout=remaining)` that resolves on the exact frame. Twilio has no equivalent single "next event" to block on. A 1s poll against an 8s+ no-input window is precise enough that this isn't worth promoting to an SLI, but it means telephony's no-input reprompt/close can lag the configured threshold by up to ~1s versus browser's exact timing.
- **Idle-frame watchdog** (`TwilioMediaBridge`, `_DEFAULT_FRAME_IDLE_TIMEOUT_S` = 30s): a new failure mode specific to telephony (Twilio Media Streams frames just stop arriving mid-call, e.g. carrier-side drop) that the browser transport has no analogue for (its heartbeat/idle-socket detection is a separate, pre-existing mechanism). Not an SLO target — flagged here so `twilio_frame_idle_timeout` log events aren't mistaken for a no-input case when triaging.

## Alerting

Unchanged from v3: latency/error thresholds, pool-checkout-wait, per-session cost ceiling. No new alert rules this cycle — the telephony parity work is feature completeness (barge-in, escalation gating, recording, hangup), not a latency or cost surface change.
