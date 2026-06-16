"""End-to-end live-call driver against the running stack (PRD §05 pipeline).

Exercises the full WebSocket pipeline through VB_Svc:
  login → start session → connect socket → greeting → multi-turn conversation
  → barge-in → escalation, printing every frame as a readable transcript.

Run (against `docker compose up`): python -m scripts.demo_call
"""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import websockets

BASE = "http://localhost:8100"
WS_BASE = "ws://localhost:8100"


def speak(text: str, *, final: bool = True) -> str:
    """Encode a 'type-to-talk' utterance the STT stub returns verbatim."""
    return base64.b64encode(b"TEXT:" + text.encode()).decode()


def banner(title: str) -> None:
    print(f"\n\033[1;34m=== {title} ===\033[0m")


def show(frame: dict) -> None:
    t = frame["type"]
    d = frame.get("data", {})
    if t == "transcript":
        who = "🤖 KOEL" if d["speaker"] == "bot" else "🧑 Customer"
        tone = f"  \033[2m[{d.get('tone')}]\033[0m" if d.get("tone") else ""
        print(f"  {who}: {d['text']}{tone}")
    elif t == "call_state":
        extra = f" reason={d['reason']}" if d.get("reason") else ""
        print(f"  \033[2m· state → {d['status']}{extra}\033[0m")
    elif t == "audio_chunk":
        print(f"  \033[2m· (audio {d.get('duration_ms')}ms)\033[0m")
    elif t == "filler":
        print(f"  \033[33m· FILLER injected: {d['text']} [{d.get('tone')}]\033[0m")
    elif t == "tts_kill":
        print(f"  \033[31m· TTS_KILL ({d['reason']}) — bot stops talking\033[0m")
    elif t == "reconnected":
        print(f"  \033[36m· RECONNECTED: {d['text']}\033[0m")
    elif t == "call_ended":
        print(
            f"  \033[1;32m· CALL ENDED — outcome={d['outcome']} "
            f"escalated={d['escalated']} turns={d['turns']}\033[0m"
        )
    elif t == "error":
        print(f"  \033[31m· ERROR {d.get('code')}: {d.get('message')}\033[0m")
    # ping frames are silent


async def drain(ws, *, seconds: float = 1.2) -> list[dict]:
    """Collect server frames for a short window, replying to pings."""
    frames: list[dict] = []
    try:
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=seconds)
            frame = json.loads(raw)
            if frame["type"] == "ping":
                await ws.send(json.dumps({"type": "pong"}))
                continue
            frames.append(frame)
            show(frame)
            if frame["type"] == "call_ended":
                break
    except TimeoutError:
        pass
    return frames


async def send_utterance(ws, text: str) -> None:
    print(f"\n  \033[2m→ customer says: \"{text}\"\033[0m")
    await ws.send(json.dumps({"type": "audio_chunk", "audio_b64": speak(text), "is_final": True}))


async def main() -> None:
    async with httpx.AsyncClient(timeout=10) as http:
        banner("1. Login (seeded demo credentials)")
        r = await http.post(f"{BASE}/v1/auth/login", json={"username": "demo", "password": "koel-demo"})
        r.raise_for_status()
        access = r.json()["access_token"]
        print(f"  access token acquired ({access[:24]}…)")
        auth = {"Authorization": f"Bearer {access}"}

        banner("2. List configured bots")
        bots = (await http.get(f"{BASE}/v1/bots", headers=auth)).json()
        for b in bots:
            print(f"  · {b['id']:16} {b['name']:22} use_case={b['use_case']} tone={b['default_tone']}")

        banner("3. Start a call session (HDFC Credit Card → Rahul Sharma)")
        r = await http.post(
            f"{BASE}/v1/sessions",
            headers=auth,
            json={"bot_id": "bot_hdfc_cc", "customer_id": "cust_cc_001"},
        )
        r.raise_for_status()
        sess = r.json()
        print(f"  session_id={sess['session_id']}")
        print(f"  CRM context: {json.dumps(sess['crm_context'], ensure_ascii=False)}")
        ws_token = sess["ws_token"]
        session_id = sess["session_id"]

    banner("4. Connect WebSocket & run the conversation")
    url = f"{WS_BASE}/ws/calls/{session_id}?token={ws_token}"
    async with websockets.connect(url) as ws:
        print("  (greeting)")
        await drain(ws)  # greeting

        # Turn 1 — interested
        await send_utterance(ws, "Haan boliye, sun raha hoon")
        await drain(ws)

        # Turn 2 — backchannel arrives WHILE bot is speaking → must NOT interrupt
        print("\n  \033[2m→ customer backchannels 'achha' mid-bot-speech (Gate 2 should ignore)\033[0m")
        await ws.send(json.dumps({
            "type": "barge_in_candidate",
            "data": {"partial_text": "achha", "speech_duration_ms": 180, "language": "hinglish"},
        }))
        await drain(ws, seconds=0.6)

        # Turn 3 — real barge-in: sustained non-backchannel speech → TTS_KILL
        print("\n  \033[2m→ customer barges in: 'ruko ruko mujhe ek sawaal hai' (Gate 2 → interrupt)\033[0m")
        await ws.send(json.dumps({
            "type": "barge_in_candidate",
            "data": {
                "partial_text": "ruko ruko mujhe ek sawaal hai",
                "speech_duration_ms": 1100,
                "language": "hinglish",
            },
        }))
        await drain(ws, seconds=0.6)
        # …and the interrupting utterance then flows through as the customer turn
        await send_utterance(ws, "Ruko, annual fee kitni hai?")
        await drain(ws)

        # Turn 4 — high intent → escalation with full-context handoff
        await send_utterance(ws, "Yes please tell me more, detail chahiye")
        await drain(ws, seconds=2.0)


if __name__ == "__main__":
    asyncio.run(main())
