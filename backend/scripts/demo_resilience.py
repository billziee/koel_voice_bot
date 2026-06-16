"""Resilience-path driver (PRD §05) against the running stack.

Exercises the conversation-resilience branches the happy-path demo does not:
  - dropped     : open a call, take a turn, drop the socket, do NOT reconnect.
                  After the grace window the call must finalise as `call_dropped`
                  with its partial transcript persisted and the CRM flagged for a
                  callback (verify via Postgres — see the companion shell step).
  - reconnect   : drop the socket, then re-bind the same session within the grace
                  window — the bot resumes with a RECONNECTED frame.
  - noinput     : stay silent — the bot re-prompts once, then closes the call as
                  `customer_unresponsive`.

Run one scenario:  python -m scripts.demo_resilience dropped|reconnect|noinput
"""

from __future__ import annotations

import asyncio
import sys

import httpx
import websockets

from scripts.demo_call import BASE, WS_BASE, banner, drain, send_utterance

BOT_ID = "bot_hdfc_cc"
CUSTOMER_ID = "cust_cc_001"


async def _session() -> tuple[str, str]:
    """Login + start a session; return (session_id, ws_token)."""
    async with httpx.AsyncClient(timeout=10) as http:
        r = await http.post(f"{BASE}/v1/auth/login", json={"username": "demo", "password": "koel-demo"})
        r.raise_for_status()
        auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
        r = await http.post(
            f"{BASE}/v1/sessions", headers=auth,
            json={"bot_id": BOT_ID, "customer_id": CUSTOMER_ID},
        )
        r.raise_for_status()
        sess = r.json()
        print(f"  session_id={sess['session_id']}")
        return sess["session_id"], sess["ws_token"]


def _url(session_id: str, ws_token: str) -> str:
    return f"{WS_BASE}/ws/calls/{session_id}?token={ws_token}"


async def scenario_dropped() -> None:
    banner("DROPPED CALL — no reconnect (expect call_dropped + CRM callback flag)")
    session_id, ws_token = await _session()
    async with websockets.connect(_url(session_id, ws_token)) as ws:
        await drain(ws)  # greeting
        await send_utterance(ws, "Haan boliye")
        await drain(ws)
        print("\n  \033[31m→ dropping the socket abruptly (no end_call)\033[0m")
        await ws.close()  # server sees WebSocketDisconnect → 15s grace begins
    print(f"\n  session {session_id} dropped; the finalizer fires after the grace TTL.")
    print(f"  SESSION_ID={session_id}")  # parsed by the shell verifier


async def scenario_reconnect() -> None:
    banner("RECONNECT within grace (expect a RECONNECTED frame on resume)")
    session_id, ws_token = await _session()
    async with websockets.connect(_url(session_id, ws_token)) as ws:
        await drain(ws)  # greeting
        await send_utterance(ws, "Haan boliye")
        await drain(ws)
        print("\n  \033[33m→ dropping the socket (will reconnect in 2s)\033[0m")
        await ws.close()

    await asyncio.sleep(2)
    print("  \033[36m→ reconnecting with the SAME session_id…\033[0m")
    async with websockets.connect(_url(session_id, ws_token)) as ws:
        frames = await drain(ws, seconds=2.0)
        ok = any(f["type"] == "reconnected" for f in frames)
        print(f"\n  {'✅' if ok else '❌'} reconnected frame received: {ok}")


async def scenario_noinput() -> None:
    banner("NO-INPUT — stay silent (expect re-prompt then customer_unresponsive)")
    session_id, ws_token = await _session()
    async with websockets.connect(_url(session_id, ws_token)) as ws:
        await drain(ws)  # greeting
        print("\n  \033[2m→ customer says nothing; answering only heartbeat pings…\033[0m")
        # Stay connected ~14s (5s re-prompt + 5s close + slack), replying to pings
        # but sending NO real input. The no-input timer must still fire.
        frames = await drain(ws, seconds=14.0)
        reprompted = any(
            f["type"] == "transcript" and "still there" in f.get("data", {}).get("text", "").lower()
            for f in frames
        )
        ended = next((f for f in frames if f["type"] == "call_ended"), None)
        print(f"\n  {'✅' if reprompted else '❌'} re-prompt sent: {reprompted}")
        print(
            f"  {'✅' if ended and ended['data']['outcome'] == 'customer_unresponsive' else '❌'} "
            f"closed as customer_unresponsive: {ended['data']['outcome'] if ended else 'no call_ended'}"
        )


SCENARIOS = {
    "dropped": scenario_dropped,
    "reconnect": scenario_reconnect,
    "noinput": scenario_noinput,
}


async def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "dropped"
    if which not in SCENARIOS:
        print(f"unknown scenario {which!r}; choose from {list(SCENARIOS)}")
        sys.exit(2)
    await SCENARIOS[which]()


if __name__ == "__main__":
    asyncio.run(main())
