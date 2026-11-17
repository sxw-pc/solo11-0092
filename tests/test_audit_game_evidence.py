from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

import pytest

from lakemirror.audit_game_evidence import AuditGameEvidenceError, inspect_audit_calibration

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "audit_game" / "calibration.json"


def test_real_conflict_codes_and_calibration_yield_prior_best_check() -> None:
    result = inspect_audit_calibration(FIXTURE)
    assert result["attack_modes"] == ["FILE_SHA256_MISMATCH", "SCHEMA_TYPE_MISMATCH", "SNAPSHOT_FORK"]
    assert [row["hits"] for row in result["calibration"]] == [[10, 0, 4], [0, 10, 4], [8, 4, 4], [2, 2, 10]]
    assert result["prior_scores"] == {
        "byte_sha_recheck": "37/50", "schema_diff": "6/25",
        "row_provenance": "17/25", "snapshot_chain": "7/25"}
    assert result["prior_best_check"] == "byte_sha_recheck"
    assert result["worst_case_policy"] is result["worst_case_value"] is None
    assert len(result["fixture_sha256"]) == 64
    assert Fraction(result["prior_scores"]["byte_sha_recheck"]) == Fraction(37, 50)


def test_unknown_mode_and_bad_hit_count_fail(tmp_path: Path) -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    file = tmp_path / "calibration.json"
    data["attack_modes"][1] = "UNKNOWN_MODE"
    file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(AuditGameEvidenceError, match="unsupported"):
        inspect_audit_calibration(file)
    data["attack_modes"][1] = "SCHEMA_TYPE_MISMATCH"
    data["calibration_hits"][0]["hits"][0] = 11
    file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(AuditGameEvidenceError, match="malformed"):
        inspect_audit_calibration(file)
