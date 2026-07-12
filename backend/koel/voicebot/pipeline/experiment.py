"""A/B script testing — deterministic variant assignment (6.3).

An experiment lives in ``BotConfig.config['ab']`` (no new table): an
``experiment_id`` and weighted ``variants``, each a partial BotConfig override
(greeting/prompt/tone/voice). At session start the caller is assigned a variant
deterministically from ``hash(experiment_id, customer_id)`` — the SAME customer
always lands in the same arm, so retries don't contaminate the split — and the
variant id is stamped onto the call for per-variant readout via the 6.2 analytics.

Resolution order is variant → bot → service default: an override only replaces
the fields it names.
"""

from __future__ import annotations

import hashlib

# Fields a variant is allowed to override (a superset would let an experiment
# change unrelated behaviour; keep it to the script/persona surface).
_OVERRIDABLE = {"greeting_template", "prompt_preamble", "default_tone", "voice", "speaking_rate"}


def _bucket(experiment_id: str, key: str, total: int) -> int:
    """Stable 0..total-1 bucket for (experiment, key). Deterministic across
    processes (sha256, not the salted built-in hash())."""
    digest = hashlib.sha256(f"{experiment_id}:{key}".encode()).hexdigest()
    return int(digest, 16) % total


def assign_variant(bot_config: dict | None, key: str | None) -> tuple[str | None, dict]:
    """Return ``(variant_id, overrides)`` for this call, or ``(None, {})`` when the
    bot has no experiment configured or it's malformed. ``variant_id`` is
    ``"<experiment_id>:<variant>"`` for the analytics readout."""
    ab = bot_config.get("ab") if isinstance(bot_config, dict) else None
    if not isinstance(ab, dict):
        return None, {}
    experiment_id = str(ab.get("experiment_id") or "").strip()
    variants = ab.get("variants")
    if not experiment_id or not isinstance(variants, list) or not variants:
        return None, {}

    weighted = []
    for v in variants:
        if not isinstance(v, dict) or not v.get("id"):
            continue
        weight = v.get("weight", 1)
        weight = int(weight) if isinstance(weight, (int, float)) and weight > 0 else 0
        if weight:
            weighted.append((str(v["id"]), weight, v.get("overrides") or {}))
    if not weighted:
        return None, {}

    total = sum(w for _, w, _ in weighted)
    target = _bucket(experiment_id, key or "anon", total)
    cursor = 0
    for vid, weight, overrides in weighted:
        cursor += weight
        if target < cursor:
            clean = {k: val for k, val in overrides.items() if k in _OVERRIDABLE} if isinstance(
                overrides, dict
            ) else {}
            return f"{experiment_id}:{vid}", clean
    # Unreachable (target < total), but stay safe.
    vid, _, overrides = weighted[-1]
    return f"{experiment_id}:{vid}", {k: v for k, v in overrides.items() if k in _OVERRIDABLE}
