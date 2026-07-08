"""Settings construction + seed data sanity (cheap coverage of config modules)."""

from __future__ import annotations

from koel.nlp.config import get_nlp_settings
from koel.stt.config import get_stt_settings
from koel.tts.config import get_tts_settings


def test_service_settings_build_with_sane_defaults():
    stt, tts, nlp = get_stt_settings(), get_tts_settings(), get_nlp_settings()
    assert stt.stt_provider in ("stub", "groq")
    assert tts.tts_provider in ("stub", "groq")
    assert nlp.rag_top_k >= 1
    # groq_enabled is a derived guard used by the provider factories.
    assert isinstance(nlp.groq_enabled, bool)


def test_ws_streaming_and_smart_turn_on_by_default():
    # Both are soak-tested and self-degrade on failure (WS→HTTP fallback,
    # smart-turn→gate-off without the model/onnxruntime), so on-by-default is
    # safe; a silent flip back to False would quietly regress latency.
    from koel.tts.config import TtsSettings
    from koel.voicebot.config import VoiceBotSettings

    assert TtsSettings().sarvam_tts_ws_enabled is True
    assert VoiceBotSettings().smart_turn_enabled is True


def test_seed_customers_cover_all_use_cases():
    from koel.crm.seed import SEED_CUSTOMERS

    use_cases = {c["use_case"] for c in SEED_CUSTOMERS}
    # Every PRD use case + a DND record for the TRAI scrub path.
    assert {"credit_card", "collections", "personal_loan", "insurance"} <= {
        uc.value if hasattr(uc, "value") else uc for uc in use_cases
    }
    assert any(c.get("dnd") for c in SEED_CUSTOMERS)
    assert all(c.get("id") and c.get("name") for c in SEED_CUSTOMERS)
