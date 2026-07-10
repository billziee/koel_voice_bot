"""DB connection-pool load-test harness (O-10 Phase 2 — MANUAL / OPS tool).

Drives N concurrent HTTP clients against a **running** backend so the async
SQLAlchemy connection pool (``koel.db.session.get_engine``) is put under real
checkout contention, then reports per-request latency percentiles (p50/p95/p99)
and error rate. Pair the latency tail with the pool metrics the engine already
emits — ``koel.db.pool.in_use`` and ``koel.db.pool.held`` (installed by
``_install_pool_metrics``) — to decide whether ``db_pool_size`` + ``db_max_overflow``
need bumping. See ``docs/LOADTEST.md`` for how to read the numbers.

Why this is NOT a CI test
-------------------------
A real connection pool only exists for the Postgres (asyncpg) engine: the test
suite runs on SQLite via ``StaticPool``, which has a single connection and no
``pool_size``/``max_overflow`` semantics, so there is nothing to load-test there.
This harness therefore needs a **real Postgres-backed deployment** (the docker
stack, staging, or the demo box) and is run by hand during Phase-2 ops work — it
is deliberately not wired into pytest/CI.

What it drives
--------------
Each client logs in once (a shared token; ``/v1/auth/login`` is per-IP
rate-limited, so we authenticate a single time and reuse the token), then loops
``--iterations`` times over a set of **DB-backed** read endpoints, each of which
opens a ``session_scope`` and therefore checks a connection out of the pool:

    GET /v1/knowledge-indexes    (KB read — GROUP BY over knowledge_chunks)
    GET /v1/knowledge-chunks     (KB read — ordered SELECT + LIMIT)
    GET /v1/bots                 (SELECT over bot_config)

With ``--with-session`` it also issues ``POST /v1/sessions`` (loads the bot row);
note that endpoint is per-IP rate-limited (20/60s), so at high concurrency from a
single host it will return HTTP 429 — realistic, but noise for pool measurement,
hence off by default.

Only ``httpx`` (already a dependency) and the stdlib are used — no new deps.

Usage
-----
    # against the docker stack / a real Postgres deployment
    python -m scripts.loadtest_pool --clients 50 --iterations 5 \
        --base http://localhost:8100

    # env-driven (e.g. from a k8s Job): KOEL_LOADTEST_CLIENTS overrides --clients
    KOEL_LOADTEST_CLIENTS=100 python -m scripts.loadtest_pool

Exit code is 0 unless login fails or every request errored (so a smoke run in a
pipeline can still detect a totally-down backend); it does NOT assert an SLO —
percentile interpretation is left to the operator per docs/LOADTEST.md.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from dataclasses import dataclass, field

import httpx

# --- Defaults (overridable via argv, and --clients via KOEL_LOADTEST_CLIENTS) ---
_DEFAULT_CLIENTS = 50
_DEFAULT_ITERATIONS = 5
_DEFAULT_BASE = "http://localhost:8100"
_DEFAULT_BOT = "bot_hdfc_cc"
_DEMO_CREDENTIALS = {"username": "demo", "password": "koel-demo"}
_HTTP_TIMEOUT_S = 30.0
_CHUNK_READ_LIMIT = 50  # rows for the /v1/knowledge-chunks read


@dataclass
class Sample:
    """One timed request outcome."""

    op: str
    latency_ms: float
    ok: bool


@dataclass
class Results:
    """Accumulated samples across all clients (aggregated after the run)."""

    samples: list[Sample] = field(default_factory=list)

    def add(self, op: str, latency_ms: float, *, ok: bool) -> None:
        self.samples.append(Sample(op=op, latency_ms=latency_ms, ok=ok))


def _percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile (0..1). Empty input ⇒ 0.0."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return ordered[index]


async def _login(base: str) -> str:
    """Authenticate once and return an access token (shared by all clients)."""
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as client:
        response = await client.post(f"{base}/v1/auth/login", json=_DEMO_CREDENTIALS)
        response.raise_for_status()
        return response.json()["access_token"]


async def _timed_get(client: httpx.AsyncClient, op: str, url: str, results: Results) -> None:
    """Issue a GET, record its latency, and flag non-2xx as an error."""
    start = time.perf_counter()
    ok = False
    try:
        response = await client.get(url)
        ok = response.is_success
    except Exception:  # noqa: BLE001 — a transport failure is just a failed sample
        ok = False
    finally:
        results.add(op, (time.perf_counter() - start) * 1000.0, ok=ok)


async def _timed_post_session(
    client: httpx.AsyncClient, base: str, bot_id: str, results: Results
) -> None:
    """POST /v1/sessions (loads the bot row from the DB). Rate-limited endpoint."""
    start = time.perf_counter()
    ok = False
    try:
        response = await client.post(f"{base}/v1/sessions", json={"bot_id": bot_id})
        ok = response.is_success
    except Exception:  # noqa: BLE001
        ok = False
    finally:
        results.add("POST /v1/sessions", (time.perf_counter() - start) * 1000.0, ok=ok)


async def _run_client(
    base: str,
    token: str,
    bot_id: str,
    iterations: int,
    with_session: bool,
    results: Results,
) -> None:
    """One concurrent client: repeat the DB-backed workload ``iterations`` times."""
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=base, headers=headers, timeout=_HTTP_TIMEOUT_S) as client:
        for _ in range(iterations):
            await _timed_get(client, "GET /v1/knowledge-indexes", "/v1/knowledge-indexes", results)
            await _timed_get(
                client,
                "GET /v1/knowledge-chunks",
                f"/v1/knowledge-chunks?limit={_CHUNK_READ_LIMIT}",
                results,
            )
            await _timed_get(client, "GET /v1/bots", "/v1/bots", results)
            if with_session:
                await _timed_post_session(client, base, bot_id, results)


async def _maybe_fetch_metrics(metrics_url: str | None) -> str | None:
    """Best-effort scrape of a metrics endpoint (e.g. a Prometheus text exporter).

    KOEL emits pool metrics over OTLP, not a built-in HTTP endpoint, so this only
    returns something if you point ``--metrics-url`` at a real scrape target.
    """
    if not metrics_url:
        return None
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as client:
            response = await client.get(metrics_url)
            response.raise_for_status()
            return response.text
    except Exception as exc:  # noqa: BLE001 — metrics are optional context
        return f"(could not fetch metrics from {metrics_url}: {exc})"


def _pool_metric_lines(metrics_text: str) -> list[str]:
    """Filter a Prometheus scrape down to the DB-pool series, if present."""
    return [
        line
        for line in metrics_text.splitlines()
        if "koel_db_pool" in line and not line.startswith("#")
    ]


def _report(results: Results, metrics_text: str | None) -> None:
    """Print per-op and overall latency percentiles + error rate."""
    total = len(results.samples)
    errors = sum(1 for s in results.samples if not s.ok)
    error_rate = errors / total if total else 1.0

    ops = sorted({s.op for s in results.samples})
    print(f"\nrequests={total}  errors={errors}  error_rate={error_rate:.2%}")
    print(f"{'operation':32} {'n':>6} {'p50':>8} {'p95':>8} {'p99':>8}  (ms)")
    print("-" * 72)
    for op in ops:
        latencies = [s.latency_ms for s in results.samples if s.op == op]
        print(
            f"{op:32} {len(latencies):>6} "
            f"{_percentile(latencies, 0.50):>8.0f} "
            f"{_percentile(latencies, 0.95):>8.0f} "
            f"{_percentile(latencies, 0.99):>8.0f}"
        )
    all_latencies = [s.latency_ms for s in results.samples]
    print("-" * 72)
    print(
        f"{'ALL':32} {total:>6} "
        f"{_percentile(all_latencies, 0.50):>8.0f} "
        f"{_percentile(all_latencies, 0.95):>8.0f} "
        f"{_percentile(all_latencies, 0.99):>8.0f}"
    )

    print("\nPool pressure (koel.db.pool.in_use / koel.db.pool.held):")
    if metrics_text is None:
        print(
            "  No --metrics-url given. Read these from your metrics backend "
            "(Grafana/Prometheus)\n"
            "  while this run executes — see docs/LOADTEST.md. A latency tail that "
            "balloons\n"
            "  under load while the per-op DB work is cheap points at pool "
            "checkout-wait."
        )
    else:
        lines = _pool_metric_lines(metrics_text)
        if lines:
            for line in lines:
                print(f"  {line}")
        else:
            print("  (no koel_db_pool_* series found at the metrics endpoint)")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--clients",
        type=int,
        default=int(os.environ.get("KOEL_LOADTEST_CLIENTS", _DEFAULT_CLIENTS)),
        help="Number of concurrent clients (env: KOEL_LOADTEST_CLIENTS).",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=int(os.environ.get("KOEL_LOADTEST_ITERATIONS", _DEFAULT_ITERATIONS)),
        help="How many times each client repeats the workload.",
    )
    parser.add_argument(
        "--base",
        default=os.environ.get("KOEL_LOADTEST_BASE", _DEFAULT_BASE),
        help="Base URL of the running backend.",
    )
    parser.add_argument("--bot", default=_DEFAULT_BOT, help="bot_id for --with-session.")
    parser.add_argument(
        "--with-session",
        action="store_true",
        help="Also POST /v1/sessions each iteration (rate-limited; see module docstring).",
    )
    parser.add_argument(
        "--metrics-url",
        default=os.environ.get("KOEL_LOADTEST_METRICS_URL"),
        help="Optional Prometheus-text metrics endpoint to scrape pool series from.",
    )
    return parser.parse_args()


async def main() -> int:
    args = _parse_args()
    print(
        f"DB-pool load test → {args.base}  clients={args.clients} "
        f"iterations={args.iterations} with_session={args.with_session}"
    )

    try:
        token = await _login(args.base)
    except Exception as exc:  # noqa: BLE001 — a down backend is an operator-facing error
        print(f"FATAL: login failed against {args.base}: {exc}")
        return 1

    results = Results()
    started = time.perf_counter()
    await asyncio.gather(
        *(
            _run_client(args.base, token, args.bot, args.iterations, args.with_session, results)
            for _ in range(args.clients)
        )
    )
    elapsed = time.perf_counter() - started

    metrics_text = await _maybe_fetch_metrics(args.metrics_url)
    _report(results, metrics_text)
    throughput = len(results.samples) / elapsed if elapsed else 0.0
    print(f"\nwall={elapsed:.1f}s  throughput={throughput:.0f} req/s")

    # Non-zero only if the whole run failed (lets a pipeline smoke-detect a dead backend);
    # percentile/SLO interpretation is intentionally left to the operator.
    all_failed = bool(results.samples) and all(not s.ok for s in results.samples)
    return 1 if (not results.samples or all_failed) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
