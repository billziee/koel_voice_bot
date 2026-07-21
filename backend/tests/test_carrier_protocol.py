"""CarrierBridge protocol conformance (roadmap 1.1 + 1.4).

``CarrierBridge`` (koel/voicebot/telephony/carrier.py) has no runtime code of
its own — it's a pure structural interface, so "coverage" on its ``...``
method bodies would be meaningless. What IS worth a real regression test:
:class:`TwilioMediaBridge` is documented (carrier.py, exotel.py) as the first
concrete implementation of that contract, and any future carrier adapter
(Exotel) is meant to satisfy it too. If ``TwilioMediaBridge``'s
``send_audio``/``send_clear``/``hangup`` are ever renamed or resignatured,
this isinstance check fails immediately instead of the interface drift going
unnoticed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from koel.voicebot.session.models import SessionState
from koel.voicebot.telephony.bridge import TwilioMediaBridge
from koel.voicebot.telephony.carrier import CarrierBridge


class _FakeTwilioWS:
    """Minimal structural ``TwilioWebSocket`` fake (mirrors ``FakeTwilioWS`` in
    test_telephony_bridge.py) — this test never drives ``run()``, so no frames
    need to be scripted."""

    async def iter_text(self) -> AsyncIterator[str]:
        for frame in ():  # type: str
            yield frame

    async def send_text(self, data: str) -> None:
        return None


class _FakeOrchestrator:
    """Minimal structural ``OrchestratorLike`` fake (mirrors
    ``RecordingOrchestrator`` in test_telephony_bridge.py) — never actually
    driven here; only its shape matters for construction."""

    async def greet(self, state, send) -> None:  # noqa: ANN001
        return None

    async def handle_final_transcript(self, state, send, stt) -> None:  # noqa: ANN001
        return None

    async def end_call(self, state, send, *, outcome: str) -> None:  # noqa: ANN001
        return None


def _state(session_id: str = "carrier_1") -> SessionState:
    return SessionState(session_id=session_id, bot_id="bot_test", use_case="credit_card")


def test_twilio_media_bridge_satisfies_the_carrier_bridge_protocol():
    """The concrete carrier adapter must structurally match the carrier-
    agnostic contract the orchestrator (and any future Exotel adapter) is
    meant to drive it through."""
    bridge = TwilioMediaBridge(
        websocket=_FakeTwilioWS(),
        orchestrator=_FakeOrchestrator(),
        state=_state(),
    )

    assert isinstance(bridge, CarrierBridge)
