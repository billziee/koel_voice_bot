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

## SLOs and alerts

| SLO (PRD §14) | Target | Metric / query | Alert |
|---|---|---|---|
| Turn latency P95 | < 300 ms | `histogram_quantile(0.95, koel_turn_latency)` | Page if P95 > 300 ms for 5 min |
| Per-hop P95 | STT/TTS < 100 ms, NLP < 200 ms | `koel_hop_latency{hop=…}` P95 | Warn on sustained breach |
| Error rate | < 1% | `rate(koel_errors) / rate(turns)` | Page if > 1% for 5 min |
| Barge-in false-interrupt | < 5% | app log ratio (interrupted turns / total) | Warn |
| Tone-tag coverage | 100% | `TranscriptTurn.tone` non-null ratio | Warn if < 100% |
| STT WER | < 0.10 (>90% acc) | `koel.stt.eval.corpus_wer` over the labelled set | CI gate + weekly batch |
| Intent accuracy | > 85% | `tests/test_intent_accuracy.py` | CI gate |
| Groq cost | budget/session | `koel.nlp.cost_usd` per session | Warn on per-session budget trip |

## Infra health (also alert)

- Redis evictions / memory pressure → session-state loss risk.
- DB connection-pool exhaustion (`db_pool_size` + `db_max_overflow`) → 5xx.
- Circuit breaker open (`koel.errors{hop=nlp}` spike + fallback logs) → upstream down.
- TTS `fallback_used` rate → primary engine degraded.

## Load target

10 concurrent WS sessions sustaining turn-latency P95 < 300 ms with error rate
< 1% and no pool exhaustion. See the load-test harness note in `tests/README`
(R) — locust/asyncio driver asserting these under concurrency.
