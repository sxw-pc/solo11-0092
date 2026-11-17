from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest

from lakemirror.audit_game_evidence import inspect_audit_calibration
from lakemirror.audit_policy_preview import preview_audit_policy

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "audit_game" / "calibration.json"


def test_prior_best_and_uniform_both_fail_half_worst_case() -> None:
    evidence = inspect_audit_calibration(FIXTURE)
    pure = preview_audit_policy(evidence, ["1", "0", "0", "0"])
    uniform = preview_audit_policy(evidence, ["1/4"] * 4)
    assert pure["worst_case_yield"] == "0"
    assert pure["prior_weighted_yield"] == "37/50"
    assert uniform["worst_case_yield"] == "2/5"
    assert pure["publishable"] is uniform["publishable"] is False


def test_feasible_nonoptimal_mix_crosses_half_without_proving_optimum() -> None:
    evidence = inspect_audit_calibration(FIXTURE)
    result = preview_audit_policy(evidence, ["0", "47/200", "118/200", "35/200"])
    assert result["by_attack_mode"] == {
        "FILE_SHA256_MISMATCH": "507/1000",
        "SCHEMA_TYPE_MISMATCH": "253/500",
        "SNAPSHOT_FORK": "101/200",
    }
    assert Fraction(result["worst_case_yield"]) == Fraction(101, 200)
    assert result["internal_preview_only"] is True
    with pytest.raises(ValueError, match="probability"):
        preview_audit_policy(evidence, ["1/4", "1/4", "1/4", "1/2"])
