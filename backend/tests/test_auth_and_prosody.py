"""JWT round-trip (PRD §08) + tone→prosody mapping (PRD §06)."""

import pytest

from koel.common.auth import decode_token, issue_token
from koel.common.config import CommonSettings
from koel.common.errors import ApiError
from koel.common.schemas import Tone
from koel.tts.providers import resolve_prosody
from koel.tts.schemas import SynthesizeRequest

SETTINGS = CommonSettings(jwt_secret="test-secret")


def test_access_token_round_trip():
    token = issue_token("demo", kind="access", settings=SETTINGS)
    claims = decode_token(token, expected_kind="access", settings=SETTINGS)
    assert claims.sub == "demo"
    assert claims.kind == "access"


def test_ws_token_rejected_as_access():
    token = issue_token("demo", kind="ws", settings=SETTINGS)
    with pytest.raises(ApiError) as exc:
        decode_token(token, expected_kind="access", settings=SETTINGS)
    assert exc.value.code == "wrong_token_kind"


def test_tampered_token_rejected():
    with pytest.raises(ApiError):
        decode_token("not.a.jwt", expected_kind="access", settings=SETTINGS)


def test_cheerful_is_brighter_than_empathetic():
    cheerful = resolve_prosody(SynthesizeRequest(session_id="s", text="hi", tone=Tone.cheerful))
    empathetic = resolve_prosody(SynthesizeRequest(session_id="s", text="hi", tone=Tone.empathetic))
    assert cheerful.pitch > empathetic.pitch
    assert cheerful.rate > empathetic.rate


def test_speaking_rate_override_multiplies_tone_rate():
    fast = resolve_prosody(
        SynthesizeRequest(session_id="s", text="hi", tone=Tone.neutral, speaking_rate=1.5)
    )
    assert fast.rate == pytest.approx(1.5)
