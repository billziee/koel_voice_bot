"""Deterministic in-call calculators (roadmap 2.2).

LLMs are unreliable at arithmetic, and a personal-loan EMI is a regulated quote
(PRD §04) — it must be exact, not "about". These pure functions compute the
figures server-side so the model only ever *quotes* a number we calculated,
never derives one itself. Dependency-free (stdlib only) so any service can
import them without pulling in NLP provider machinery.
"""

from __future__ import annotations

# Standard reducing-balance EMI. Tenures we pre-compute for "what-if" grounding
# so the bot can answer "what about 48 months?" from a table instead of doing
# (or hallucinating) the maths mid-call.
_DEFAULT_TENURES_MONTHS = (12, 24, 36, 48, 60, 72)
_MAX_TENURE_MONTHS = 600  # 50y — guard against absurd inputs producing junk


def emi(principal: float, annual_rate_pct: float, months: int) -> float:
    """Monthly EMI for a reducing-balance loan, rounded to the rupee.

    ``annual_rate_pct`` is the annual interest rate as a percentage (e.g. 10.5).
    A 0% rate degrades to simple principal/tenure. Raises ``ValueError`` on
    non-positive principal/tenure so callers fail loudly rather than quote junk.
    """
    if principal <= 0:
        raise ValueError("principal must be positive")
    if months <= 0 or months > _MAX_TENURE_MONTHS:
        raise ValueError("months must be in (0, 600]")
    if annual_rate_pct < 0:
        raise ValueError("annual_rate_pct must be non-negative")

    monthly_rate = annual_rate_pct / 12.0 / 100.0
    if monthly_rate == 0.0:
        return round(principal / months)

    growth = (1.0 + monthly_rate) ** months
    payment = principal * monthly_rate * growth / (growth - 1.0)
    return round(payment)


def emi_breakdown(principal: float, annual_rate_pct: float, months: int) -> dict[str, float]:
    """EMI plus what it totals to — the figures a customer actually asks about."""
    monthly = emi(principal, annual_rate_pct, months)
    total_payable = round(monthly * months)
    return {
        "emi": monthly,
        "total_payable": total_payable,
        "total_interest": round(total_payable - principal),
    }


def emi_options(
    principal: float,
    annual_rate_pct: float,
    tenures_months: tuple[int, ...] = _DEFAULT_TENURES_MONTHS,
) -> dict[int, float]:
    """EMI for each of several tenures, for what-if grounding.

    Silently skips any tenure ``emi()`` rejects, so one bad entry never sinks
    the whole table. Returns an empty dict if principal/rate are unusable.
    """
    out: dict[int, float] = {}
    for months in tenures_months:
        try:
            out[months] = emi(principal, annual_rate_pct, months)
        except ValueError:
            continue
    return out


def format_inr(amount: float) -> str:
    """Indian-grouped rupee string, e.g. 1250000 -> '₹12,50,000' (no paise)."""
    n = int(round(amount))
    sign = "-" if n < 0 else ""
    digits = str(abs(n))
    if len(digits) <= 3:
        grouped = digits
    else:
        head, tail = digits[:-3], digits[-3:]
        # Indian grouping: last 3 digits, then pairs (12,34,567).
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        grouped = ",".join(parts) + "," + tail
    return f"{sign}₹{grouped}"
