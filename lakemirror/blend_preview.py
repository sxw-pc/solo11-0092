"""Internal evaluation of a specified blend; no cost optimization."""
from __future__ import annotations

from fractions import Fraction
from typing import Any


def preview_blend(evidence: dict[str, Any], amounts: dict[str, str]) -> dict[str, Any]:
    lots = evidence["lots"]
    if set(amounts) != {row["lot"] for row in lots}:
        raise ValueError("blend lot set differs")
    weights = {lot: Fraction(value) for lot, value in amounts.items()}
    if any(value < 0 for value in weights.values()) or sum(weights.values()) != 1:
        raise ValueError("blend amounts do not form one batch")
    cost = sum(weights[row["lot"]] * Fraction(row["cost_cents"]) for row in lots)

    def potency(delta: Fraction) -> Fraction:
        return sum(weights[row["lot"]] *
                   (Fraction(row["base_potency"]) + Fraction(row["bias_slope"]) * delta)
                   for row in lots)

    lower, upper = Fraction(evidence["bias_minimum"]), Fraction(evidence["bias_maximum"])
    independent_low = sum(weights[row["lot"]] * min(
        Fraction(row["base_potency"]) + Fraction(row["bias_slope"]) * lower,
        Fraction(row["base_potency"]) + Fraction(row["bias_slope"]) * upper)
        for row in lots)
    return {
        "internal_preview_only": True,
        "publishable": False,
        "amounts": {key: str(value) for key, value in weights.items()},
        "cost_cents": str(cost),
        "nominal_potency": str(potency(Fraction(0))),
        "potency_at_bias_minimum": str(potency(lower)),
        "potency_at_bias_maximum": str(potency(upper)),
        "independent_per_lot_lower_potency": str(independent_low),
    }
