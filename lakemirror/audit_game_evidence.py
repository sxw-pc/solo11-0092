"""Validate synthetic calibration for scheduling one lake audit check.

This evidence reports empirical hit fractions and a simple prior-weighted
diagnostic. It does not optimize or certify a robust inspection strategy.
"""
from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any

from .util import CONFLICT_CODES, sha256_file


class AuditGameEvidenceError(ValueError):
    pass


def inspect_audit_calibration(path: str | Path) -> dict[str, Any]:
    file = Path(path).resolve()
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AuditGameEvidenceError("invalid audit calibration") from exc
    modes = data.get("attack_modes")
    prior = data.get("case_prior_counts")
    rows = data.get("calibration_hits")
    count = data.get("repetitions_per_cell")
    if (data.get("schema_version") != 1 or not isinstance(data.get("snapshot_id"), str)
            or modes != ["FILE_SHA256_MISMATCH", "SCHEMA_TYPE_MISMATCH", "SNAPSHOT_FORK"]
            or any(mode not in CONFLICT_CODES for mode in modes)
            or not isinstance(prior, list) or len(prior) != len(modes)
            or any(type(value) is not int or value < 0 for value in prior) or sum(prior) != 10
            or data.get("one_check_per_window") is not True
            or count != 10 or not isinstance(rows, list) or len(rows) != 4
            or data.get("contracts") != ["worst_case_guarantee", "prior_weighted_yield"]):
        raise AuditGameEvidenceError("unsupported audit calibration contract")
    seen = set()
    probabilities = []
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("check"), str)
                or row["check"] in seen or not isinstance(row.get("hits"), list)
                or len(row["hits"]) != len(modes)
                or any(type(value) is not int or value < 0 or value > count for value in row["hits"])):
            raise AuditGameEvidenceError("malformed audit hit row")
        seen.add(row["check"])
        probabilities.append({"check": row["check"], "hits": row["hits"],
                              "rates": [str(Fraction(value, count)) for value in row["hits"]]})
    if seen != {"byte_sha_recheck", "schema_diff", "row_provenance", "snapshot_chain"}:
        raise AuditGameEvidenceError("missing audit check")
    prior_scores = {
        row["check"]: str(sum(Fraction(hit, count) * Fraction(weight, 10)
                              for hit, weight in zip(row["hits"], prior)))
        for row in rows
    }
    best = max(prior_scores, key=lambda key: (Fraction(prior_scores[key]), key))
    return {
        "snapshot_id": data["snapshot_id"],
        "fixture_sha256": sha256_file(file),
        "attack_modes": modes,
        "case_prior_counts": prior,
        "one_check_per_window": True,
        "calibration": probabilities,
        "prior_scores": prior_scores,
        "prior_best_check": best,
        "worst_case_policy": None,
        "worst_case_value": None,
    }
