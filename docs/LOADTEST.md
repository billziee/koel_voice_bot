# KOEL — DB Connection-Pool Load Test (O-10 Phase 2)

How to load-test the async SQLAlchemy connection pool, read the pool metrics, and
decide when to resize it. This is a **manual / ops** procedure — see "Why not CI"
below. Harness: [`backend/scripts/loadtest_pool.py`](../backend/scripts/loadtest_pool.py).

## Why this is not a CI test

A real connection pool only exists on the **Postgres (asyncpg)** engine. The pytest
suite runs on SQLite via `StaticPool` — one connection, no `pool_size` /
`max_overflow` semantics — so there is nothing to load-test in CI. The harness needs
a running, Postgres-backed deployment (the docker stack, staging, or the demo box)
and is run by hand during Phase-2 ops work.

## How the pool is configured

`koel.db.session.get_engine` builds the engine (Postgres only) from
`koel.common.config`:

| Setting | Default | Meaning |
|---|---|---|
| `db_pool_size` | 10 | steady connections kept open per worker |
| `db_max_overflow` | 5 | extra burst connections above `pool_size` |
| `db_pool_timeout_s` | 30 | seconds a request waits for a free connection before erroring |
| `db_pool_recycle_s` | 1800 | recycle a connection after this long (beat RDS/proxy idle close) |
| `db_pool_use_lifo` | true | LIFO checkout so idle connections age out between bursts |
| `pool_pre_ping` | on | reap connections the server closed underneath us |

**Sizing math:** peak live connections for one instance =
`(db_pool_size + db_max_overflow) × uvicorn_workers`. Summed across every service
instance, that product must stay under RDS `max_connections` (leave headroom for
migrations, `psql`, and other clients). Defaults give a 1-worker instance 15 peak
connections.

## Running the harness

Point it at a **running** backend. It logs in once (the shared demo token — login is
per-IP rate-limited), then fans out N concurrent clients that each loop over
DB-backed reads (`/v1/knowledge-indexes`, `/v1/knowledge-chunks`, `/v1/bots`), every
call opening a `session_scope` that checks a connection out of the pool.

```bash
cd backend
# 50 concurrent clients × 5 iterations against the docker stack
python -m scripts.loadtest_pool --clients 50 --iterations 5 --base http://localhost:8100

# env-driven (e.g. a k8s Job)
KOEL_LOADTEST_CLIENTS=100 python -m scripts.loadtest_pool
```

Key flags (all have env equivalents; see `--help`):

- `--clients` / `KOEL_LOADTEST_CLIENTS` — concurrent clients (default 50).
- `--iterations` — workload repeats per client (default 5).
- `--base` / `KOEL_LOADTEST_BASE` — backend base URL.
- `--with-session` — also `POST /v1/sessions` each iteration. That endpoint is
  per-IP rate-limited (20/60s), so from a single host at high concurrency it will
  return HTTP 429; useful to see, but noise for pure pool measurement, so it is off
  by default.
- `--metrics-url` / `KOEL_LOADTEST_METRICS_URL` — optional Prometheus-text endpoint
  to scrape the `koel_db_pool_*` series from and print alongside the latency table.

### Sample output

```
requests=750  errors=0  error_rate=0.00%
operation                             n      p50      p95      p99  (ms)
------------------------------------------------------------------------
GET /v1/bots                        250       12       28       41
GET /v1/knowledge-chunks            250       18       39       58
GET /v1/knowledge-indexes           250       11       25       37
------------------------------------------------------------------------
ALL                                 750       14       33       55

wall=2.1s  throughput=357 req/s
```

## Interpreting pool metrics

The engine already emits two histograms (installed by `_install_pool_metrics`,
exported over OTLP → collector → Prometheus/Grafana):

| Metric | Meaning | What to watch |
|---|---|---|
| `koel.db.pool.in_use` | connections checked out, sampled at each checkout | the **ceiling** = `db_pool_size + db_max_overflow`. Sitting at the ceiling ⇒ pool saturated. |
| `koel.db.pool.held` | how long a connection stayed checked out (checkout→checkin), ms | a rising p95 means queries hold connections longer, so fewer are free for the next request. |

Run the harness and, **during the run**, read these from your metrics backend. There
is no built-in `/metrics` HTTP endpoint (metrics go out via OTLP), so
`--metrics-url` only helps if you have a separate Prometheus-text scrape target.
Example Prometheus queries:

```promql
histogram_quantile(0.95, sum(rate(koel_db_pool_in_use_bucket[1m])) by (le))
histogram_quantile(0.95, sum(rate(koel_db_pool_held_bucket[1m]))  by (le))
```

**Reading the two signals together:**

- `in_use` **below** the ceiling and a flat latency tail → the pool is not the
  bottleneck. Any latency is query/CPU/network, not checkout-wait.
- `in_use` **pinned at** the ceiling while the harness's p95/p99 latency balloons
  (far above `koel.db.pool.held`) → requests are **waiting for a connection**
  (checkout-wait). The pool is the bottleneck.
- Requests erroring after ~`db_pool_timeout_s` with `in_use` at the ceiling →
  outright **pool exhaustion** (`QueuePool limit ... connection timed out`).

## When to bump the pool (and when not to)

**Bump `db_pool_size` (and/or `db_max_overflow`) when:**

- `koel.db.pool.in_use` sits at the ceiling at your target concurrency, **and**
- the latency tail is dominated by checkout-wait (p99 ≫ `koel.db.pool.held` p99).

Raise `db_pool_size` first (steady capacity); use `db_max_overflow` for burst
headroom above it. After bumping, **recompute the sizing math** and confirm
`(db_pool_size + db_max_overflow) × workers × instances` still fits under RDS
`max_connections` with headroom.

**Do NOT just bump the pool when:**

- `koel.db.pool.held` p95 is high → the real fix is faster/shorter transactions,
  not more connections (more connections against a busy DB can make contention
  worse).
- You are already near the RDS connection budget → raise RDS `max_connections`,
  add **pgbouncer** (transaction pooling), or scale down workers instead.

**Other levers before resizing:**

- `db_pool_use_lifo=true` (default) keeps the idle-connection footprint small
  between bursts — leave it on unless you have a specific reason to round-robin.
- `db_pool_timeout_s` is a backstop, not a latency knob. Lower it only if you would
  rather fail-fast with a 5xx than queue briefly under a burst.

> Note: the WebSocket turn-latency load test is a separate concern — see
> [`backend/scripts/loadtest.py`](../backend/scripts/loadtest.py) and the load-target
> section of [`SLO.md`](./SLO.md).
