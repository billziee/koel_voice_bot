# KOEL — Service Level Objectives & Alerting

Ties the PRD §14 NFR targets to the metrics KOEL emits (Q) and the alerts that
should fire on breach. Metrics are OpenTelemetry instruments (`koel.common.
telemetry`); export over OTLP → collector → Prometheus/Grafana in production.

## Emitted metrics

| Instrument | Type | Labels | Meaning |
|---|---|---|---|
| `koel.turn.latency` | histogram (ms) | — | Audio-in → audio-out per customer turn |
| `koel.hop.latency` | histogram (ms) | `hop`=stt\|nlp\|tts | Per-hop latency |
| `koel.errors` | counter | `hop` | Pipeline errors by hop |
| `koel.nlp.tokens` | counter | — | Groq tokens consumed |
| `koel.nlp.cost_usd` | counter | — | Estimated Groq spend |

Distributed traces (spans) carry `synthesize_latency_ms`, `fallback_used`, and
`sample_rate` on `tts.synthesize`, and stitch FE → VB_Svc → AI over W3C
`traceparent`.

> **Honest floor:** with users in India and inference in the US, ~250 ms of
> trans-Pacific RTT is unavoidable, so a literal 300 ms *full turn* is physically
> impossible. The SLO is split: **perceived** first-audio < 300 ms (met by
> sentence-streaming TTS + filler), and a realistic **true** full-turn target.

## SLOs and alerts

| SLO (PRD §14) | Target | Metric / query | Alert |
|---|---|---|---|
| **Perceived** first-audio P95 | < 300 ms (filler + sentence-streaming) | `histogram_quantile(0.95, koel_first_audio_latency)` | Page if > 300 ms for 5 min |
| **True** full-turn P95 | ~0.8–1.3 s US-colocated (physical floor ≈ ocean RTT + STT+LLM+TTS) | `histogram_quantile(0.95, koel_turn_latency)` | Warn on regression |
| Per-hop P95 | STT/TTS < 100 ms (US-local), NLP < 200 ms (8b), tts_first_audio tracked | `koel_hop_latency{hop=…}` P95 | Warn on sustained breach |
| Error rate | < 1% | `rate(koel_errors) / rate(turns)` | Page if > 1% for 5 min |
| Barge-in false-interrupt | < 5% | app log ratio (interrupted turns / total) | Warn |
| Tone-tag coverage | 100% | `TranscriptTurn.tone` non-null ratio | Warn if < 100% |
| STT WER | < 0.10 (>90% acc) | `koel.stt.eval.corpus_wer` over the labelled set | CI gate + weekly batch |
| Intent accuracy | > 85% | `tests/test_intent_accuracy.py` | CI gate |
| Groq cost | budget/session | `koel.nlp.cost_usd` per session | Warn on per-session budget trip |

## Infra health (also alert)

- Redis evictions / memory pressure → session-state loss risk.
- DB connection-pool exhaustion (`db_pool_size` + `db_max_overflow`) → 5xx.
  Watch `koel.db.pool.in_use` (vs the pool ceiling) + `koel.db.pool.held`; load-test
  and resize per [`LOADTEST.md`](./LOADTEST.md).
- Circuit breaker open (`koel.errors{hop=nlp}` spike + fallback logs) → upstream down.
- TTS `fallback_used` rate → primary engine degraded.

## Load target

10 concurrent WS sessions sustaining turn-latency P95 < 300 ms with error rate
< 1% and no pool exhaustion. See the load-test harness note in `tests/README`
(R) — locust/asyncio driver asserting these under concurrency.

### Measured (scripts/loadtest.py, 10 sessions × 3 turns, vs the live Render US-East backend)

| Metric | Result | Verdict |
|---|---|---|
| Error rate | **0.00%** (0/30 turns) | ✅ meets <1% — capacity + reliability under 10 concurrent hold on the 512 MB free tier |
| First-audio P50 | ~839 ms | the realistic uncontended floor (STT+NLP+TTS + one ocean round-trip) |
| First-audio P95 | ~2340 ms | 🔴 far over the 300 ms *literal* budget — ocean RTT + real US inference + free-tier contention |
| First-audio P99 | ~6132 ms | tail under load (single-process free tier) |

The **300 ms is unreachable from an India client against US inference** (confirmed, not assumed). What IS proven: the pipeline is error-free at the PRD's 10-concurrent target. Closing the latency gap needs in-region GPU inference (which trades against DPDP residency) — an infra decision, not a code fix.

### Expected effect of Sarvam WS streaming + smart-turn (both enabled by default this cycle)

The numbers above were measured with `KOEL_SARVAM_TTS_WS_ENABLED` and
`KOEL_SMART_TURN_ENABLED` both **off** (their defaults at the time of that
run). Both are now **on by default**. Expected, not yet re-measured, effect:

- **Sarvam WS streaming TTS** removes the "wait for a full HTTP round-trip per
  sentence" cost for Hindi/Hinglish turns — audio starts arriving while
  synthesis is still running rather than after it completes. This should
  primarily improve **first-audio latency on hi/hinglish turns**, which
  previously paid the full sentence-synthesis time before any audio played.
- **Smart-turn v3.2** does not reduce latency — if anything it can *add* up to
  `smart_turn_hold_ms` (1500ms) in the specific case where the model judges an
  utterance mid-thought. Its purpose is conversation *quality* (not
  interrupting a speaker who paused) at a bounded latency cost, not a speed
  optimization.
- **Action item**: re-run `scripts/loadtest.py` against the current deploy and
  update the table above with fresh numbers — this has not been done since the
  defaults changed. See `FUTURE_PROCEEDINGS.md` for the broader latency
  roadmap (Part A) that builds on this baseline.
