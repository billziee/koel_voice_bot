"""The single-process combined app mounts all services (deploy shape)."""

from __future__ import annotations


def test_combined_mounts_all_services():
    from koel.combined import app

    mounts = {r.path for r in app.routes if getattr(r, "path", None)}
    # voicebot at "/" (catch-all) + the four AI/CRM services under prefixes.
    for prefix in ("/stt", "/nlp", "/tts", "/crm"):
        assert prefix in mounts, f"missing mount {prefix}"
    assert app.router.lifespan_context is not None
