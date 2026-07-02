"""Concurrent WS load test (R, PRD §14 P95 < 300 ms under load).

Drives N concurrent call sessions against a running stack: POST /v1/sessions,
open the WS with the token subprotocol, send a few text turns, and measure
per-turn round-trip latency (client send → first audio_chunk). Reports P50/P95/
P99 + error rate and exits non-zero if the P95 budget or error-rate bar is missed.

Usage (against the docker stack):
    python -m scripts.loadtest --sessions 10 --turns 5 --base http://localhost:8100
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

import httpx
import websockets

_DEMO = {"username": "demo", "password": "koel-demo"}
_TURN_BUDGET_MS = 300.0
_ERROR_BAR = 0.01


async def _login(base: str) -> str:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(f"{base}/v1/auth/login", json=_DEMO)
        r.raise_for_status()
        return r.json()["access_token"]


async def _start_session(base: str, token: str, bot_id: str, customer_id: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(
            f"{base}/v1/sessions",
            headers={"Authorization": f"Bearer {token}"},
            json={"bot_id": bot_id, "customer_id": customer_id},
        )
        r.raise_for_status()
        return r.json()


async def _run_session(base: str, ws_base: str, token: str, bot_id: str,
                       customer_id: str, turns: int, latencies: list[float], errors: list[int]) -> None:
    try:
        sess = await _start_session(base, token, bot_id, customer_id)
    except Exception:
        errors.append(1)
        return
    url = f"{ws_base}/ws/calls/{sess['session_id']}"
    subprotocols = ["koel.v1", f"koel.token.{sess['ws_token']}"]
    import base64 as _b64

    async def drain_until_listening(ws) -> None:  # noqa: ANN001
        # The server greets on connect (call_state → transcript → audio_chunk …).
        # Wait until it's done (call_state=listening) before timing customer turns.
        while True:
            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            if frame.get("type") == "ping":
                await ws.send(json.dumps({"type": "pong"}))
            elif frame.get("type") == "call_state" and frame.get("data", {}).get("status") == "listening":
                return

    try:
        async with websockets.connect(url, subprotocols=subprotocols) as ws:
            await drain_until_listening(ws)
            for i in range(turns):
                payload = _b64.b64encode(f"TEXT:haan mujhe offer ke baare mein batao {i}".encode()).decode()
                t0 = time.monotonic()
                # ClientMessage: type=audio_chunk with top-level fields (protocol.py).
                await ws.send(json.dumps(
                    {"type": "audio_chunk", "audio_b64": payload, "is_final": True, "audio_format": "wav"}
                ))
                # First bot audio_chunk after our turn = first-audio latency.
                while True:
                    frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                    t = frame.get("type")
                    if t == "audio_chunk":
                        latencies.append((time.monotonic() - t0) * 1000)
                        break
                    if t == "ping":
                        await ws.send(json.dumps({"type": "pong"}))
                    elif t in ("call_ended", "error"):
                        errors.append(1)
                        return
    except Exception:
        errors.append(1)


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, int(len(s) * p))]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=10)
    ap.add_argument("--turns", type=int, default=5)
    ap.add_argument("--base", default="http://localhost:8100")
    ap.add_argument("--bot", default="bot_hdfc_cc")
    ap.add_argument("--customer", default="cust_cc_001")
    args = ap.parse_args()
    ws_base = args.base.replace("http", "ws", 1)

    token = await _login(args.base)
    latencies: list[float] = []
    errors: list[int] = []
    await asyncio.gather(*(
        _run_session(args.base, ws_base, token, args.bot, args.customer, args.turns, latencies, errors)
        for _ in range(args.sessions)
    ))

    total = len(latencies) + len(errors)
    err_rate = len(errors) / total if total else 1.0
    p95 = _pct(latencies, 0.95)
    print(f"turns={len(latencies)} errors={len(errors)} err_rate={err_rate:.2%}")
    print(f"latency ms  p50={_pct(latencies,0.5):.0f}  p95={p95:.0f}  p99={_pct(latencies,0.99):.0f}")

    ok = p95 <= _TURN_BUDGET_MS and err_rate <= _ERROR_BAR and latencies
    print("RESULT:", "PASS" if ok else "FAIL",
          f"(budget p95<={_TURN_BUDGET_MS:.0f}ms, err<={_ERROR_BAR:.0%})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
