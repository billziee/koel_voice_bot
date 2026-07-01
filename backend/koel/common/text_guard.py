"""Prompt-injection sanitization, output PII redaction, context trimming.

These guard the boundary between untrusted caller speech (STT output, CRM
free-text) and the LLM prompt, and between the LLM's spoken reply and the TTS
egress. All functions are pure and defensive — they never raise on odd input
(a live turn must never be dropped, PRD §12).
"""

from __future__ import annotations

import re

from koel.common.logging import mask_account

# Role markers an attacker might inject to escape the "this is data" framing
# ("system:", "assistant:", "<|system|>", "[INST]", …). We neutralise the
# delimiter, not the word, so the caller's actual speech is preserved.
_ROLE_MARKER = re.compile(
    r"(?i)(?:^|\n)\s*(?:<\|?)?(system|assistant|user|developer|tool)(?:\|?>)?\s*[:>]",
)
_CODE_FENCE = re.compile(r"(?:`{3,}|~{3,})")
# C0 control chars (keep \t \n \r) + zero-width / BOM / directional marks.
_CONTROL = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f"
    "​-‏‪-‮⁠﻿]"
)
_WS = re.compile(r"[ \t]{3,}")
_NL = re.compile(r"\n{3,}")

# Digit runs long enough to be an account / card / phone number. Short numbers
# (fees "500", rewards "4x", "2 cards") are legitimate product facts — left alone.
# Matches 8+ digits, allowing space/dash grouping (card "1234 5678 9012 3456").
_LONG_NUMBER = re.compile(r"\b\d(?:[\d \-]{6,})\d\b")

_MAX_UNTRUSTED_CHARS = 2000
_DEFAULT_HISTORY_CHARS = 1600


def sanitize_untrusted(text: str | None) -> str:
    """Neutralise prompt-injection vectors in caller speech / CRM free-text.

    Strips role-marker delimiters, code fences, control/zero-width characters,
    collapses runaway whitespace, and caps length. This is defence-in-depth on
    top of structural delimiting + a system-prompt rule that content after the
    CUSTOMER marker is untrusted data, never instructions.
    """
    if not text:
        return ""
    out = _CONTROL.sub("", text)
    out = _CODE_FENCE.sub(" ", out)
    out = _ROLE_MARKER.sub(lambda m: " " + m.group(1) + " ", out)
    out = _WS.sub("  ", out)
    out = _NL.sub("\n\n", out).strip()
    if len(out) > _MAX_UNTRUSTED_CHARS:
        out = out[:_MAX_UNTRUSTED_CHARS].rstrip() + "…"
    return out


def sanitize_crm(crm: dict) -> dict:
    """Escape CRM values before they enter the prompt as key=value pairs."""
    return {k: sanitize_untrusted(str(v)) for k, v in crm.items() if v not in (None, "")}


def redact_pii(text: str | None) -> str:
    """Mask account/card/phone-length digit runs in a spoken reply before TTS.

    Conservative by design: only runs of 8+ digits (optionally space/dash
    grouped) are masked, so genuine product figures (₹500 fee, 4x rewards,
    2 cards) survive. Defends against the bot ever reading a caller's account
    number back over the wire (PRD §11 / §14 PII posture).
    """
    if not text:
        return ""

    def _mask(m: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) < 8:  # grouping matched but too few real digits
            return m.group(0)
        return mask_account(digits)

    return _LONG_NUMBER.sub(_mask, text)


def truncate_history(history: list[dict], max_chars: int = _DEFAULT_HISTORY_CHARS) -> list[dict]:
    """Keep the most-recent turns within a char budget (token-aware-ish).

    Replaces a fixed ``history[-8:]`` slice: a few long turns can blow the
    context / cost budget while eight one-word turns waste it. Walks backwards
    accumulating rendered length, preserving chronological order in the result.
    """
    kept: list[dict] = []
    used = 0
    for turn in reversed(history):
        rendered = len(str(turn.get("text", ""))) + 12  # speaker label + separators
        if kept and used + rendered > max_chars:
            break
        kept.append(turn)
        used += rendered
    kept.reverse()
    return kept
