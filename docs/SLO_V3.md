# KOEL — Service Level Objectives & Alerting v3

> Supersedes [SLO.md](./SLO.md). v3 reflects the streaming latency arc and the new
> cost/pool signals.

## Objectives (v1 → v2 → v3)

| SLI | v1 target | v2 measured | v3 |
|---|---|---|---|
| First-audio p50 | <300ms (turn) | ~1.0s (perceived <300ms via filler) | **token-bound** (O-2 NLP stream + O-6 STT partials) → toward ~0.5–0.7s true (US inference) |
| First-audio p95 | — | ~2.1–2.3s | shrinks with streaming; filler still masks tail |
| Turn error rate | — | 0% @ 10 concurrent | preserved; every fast path degrades to the proven batch path |
| Concurrency correctness | design | single-worker | **>1 worker correct (O-8 Redis limiter)** + observable (O-10 pool) |
| Per-call cost | — | — | **estimated per turn; hard per-session ceiling (O-9)** |

## Metrics (v3 additions to the existing hop/turn latency + error counters)
`tts_first_audio` (streaming first-chunk mark), `koel.db.pool.in_use` /
`koel.db.pool.held` (O-10 pool pressure), per-call cost + running-total (O-9),
NLP-stream vs batch path counters. All emitted via OTel; a collector/Grafana is
still Masterplan Phase-1 in this environment.

## Alerting
Unchanged thresholds from v2 for latency/error; add: pool-checkout-wait sustained
> budget ⇒ scale pool/workers (see [LOADTEST.md](./LOADTEST.md)); per-session cost
ceiling hit ⇒ investigate prompt/turn loops.
