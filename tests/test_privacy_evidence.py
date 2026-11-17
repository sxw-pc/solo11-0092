from __future__ import annotations

from pathlib import Path

import pytest

from lakemirror.privacy_evidence import PrivacyEvidenceError, inspect_privacy_table

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "privacy_table"


def test_complete_table_margins_and_sensitive_cells_are_bound() -> None:
    result = inspect_privacy_table(FIXTURES / "contract.json")
    assert result["row_totals"] == {"R1": 10, "R2": 10}
    assert result["col_totals"] == {"C1": 7, "C2": 7, "C3": 6}
    assert result["grand_total"] == 20
    assert [(x["row"], x["col"]) for x in result["cells"] if x["count"] < 2] == [
        ("R1", "C1"), ("R2", "C3")]
    assert result["feasible_sensitive_values"] is None
    assert result["suppression_masks"] is None
    assert result["publication_decisions"] is None
    assert result == inspect_privacy_table(FIXTURES / "contract.json")
    corrected = inspect_privacy_table(FIXTURES / "corrected-contract.json")
    assert corrected["parent_snapshot_id"] == result["snapshot_id"]
    assert corrected["row_totals"] == {"R1": 13, "R2": 10}
    assert corrected["col_totals"] == {"C1": 7, "C2": 10, "C3": 6}


def test_bad_table_sha_blocks() -> None:
    with pytest.raises(PrivacyEvidenceError, match="SHA differs"):
        inspect_privacy_table(FIXTURES / "bad-sha-contract.json")
