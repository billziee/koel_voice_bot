# KOEL — Deploy (Render) v3

> Supersedes [DEPLOY.md](./DEPLOY.md). Base topology unchanged; v3 reconciles the
> deploy config and adds the optional streaming/scale dependencies.

## Deploy evolution (v1 → v2 → v3)

| Aspect | v1 | v2 | v3 |
|---|---|---|---|
| Runtime | Docker Compose (dev) | Render, `koel.combined:app` from `main`, self-migrating | unchanged |
| Config parity | — | render.yaml drift (env-prefix no-ops) | **Q-6: render.yaml ↔ Dockerfile reconciled** (correct `KOEL_` prefixes, fetch-script) |
| Keep-alive | — | in-app self-ping + GH Actions cron | unchanged |
| Redis | — | — | **optional (`KOEL_RATE_LIMIT_BACKEND=redis` + `rediss://` URL) for multi-worker (O-8)** |
| Deepgram STT | — | — | **optional streaming STT creds + per-bot `stream_stt`/mode (O-6)** |
| Local embedder | — | — | **optional `koel[local-embed]` + `KOEL_NLP_EMBEDDER=fastembed` (O-4)** |
| BYOK | — | — | **model-connection config (encrypted secrets)** |
| Model prefetch | smart-turn ONNX fetch script | same | + FastEmbed model prefetch note |

## v3 env additions (all default-off / inert)
`stream_nlp`, `stream_stt` + Deepgram creds, `nlp_embedder=fastembed` +
`fastembed_model`, `rate_limit_backend=redis` + `redis_url`, `session_budget_usd`,
`pgvector_hnsw_ef_search`, `db_pool_use_lifo`/`db_pool_*`, `tts_cache_ttl_s`, BYOK
connection settings. Enabling any is additive; the v2 free-tier deploy is unchanged
if none are set. Production hardening: [SECURITY_V3.md](./SECURITY_V3.md); load
sizing: [LOADTEST.md](./LOADTEST.md).
