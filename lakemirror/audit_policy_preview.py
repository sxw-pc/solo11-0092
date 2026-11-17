"""Internal evaluation of a caller-supplied audit mix, without optimization."""
from __future__ import annotations

from fractions import Fraction
from typing import Any


def preview_audit_policy(evidence: dict[str, Any], weights: list[str]) -> dict[str, Any]:
    rows = evidence["calibration"]
    if len(weights) != len(rows):
        raise ValueError("audit strategy length differs")
    try:
        chances = [Fraction(value) for value in weights]
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError("invalid audit strategy fraction") from exc
    if any(value < 0 for value in chances) or sum(chances) != 1:
        raise ValueError("audit strategy is not a probability distribution")
    rates = [[Fraction(value) for value in row["rates"]] for row in rows]
    by_mode = [sum(chances[i] * rates[i][j] for i in range(len(rows)))
               for j in range(len(evidence["attack_modes"]))]
    prior = evidence["case_prior_counts"]
    weighted = sum(value * Fraction(count, sum(prior)) for value, count in zip(by_mode, prior))
    return {
        "internal_preview_only": True,
        "publishable": False,
        "strategy": {row["check"]: str(value) for row, value in zip(rows, chances)},
        "by_attack_mode": {mode: str(value) for mode, value in zip(evidence["attack_modes"], by_mode)},
        "worst_case_yield": str(min(by_mode)),
        "prior_weighted_yield": str(weighted),
    }
