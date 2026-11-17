from __future__ import annotations

from pathlib import Path

import pytest

from lakemirror.irr_evidence import IrrEvidenceError, inspect_irr_evidence

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "project_irr"


def test_base_and_corrected_cash_flow_bytes_are_bound() -> None:
    base = inspect_irr_evidence(FIXTURES / "base-manifest.json")
    corrected = inspect_irr_evidence(FIXTURES / "corrected-manifest.json")
    assert base["snapshot_id"] == corrected["parent_snapshot_id"]
    assert base["project_id"] == corrected["project_id"] == "SYN-PROJECT-1"
    assert [x["amount_cents"] for x in base["cashflows"]] == [-500000, 1800000, -2155000, 858000]
    assert [x["amount_cents"] for x in corrected["cashflows"]] == [-500000, 1800000, -2155000, 859000]
    assert base["hurdle_bps"] == corrected["hurdle_bps"] == 2500
    assert base["npv_at_hurdle"] is None
    assert base["admissible_irr_roots"] is None
    assert base["investment_decisions"] is None
    assert base == inspect_irr_evidence(FIXTURES / "base-manifest.json")


def test_cashflow_hash_or_row_count_disagreement_blocks() -> None:
    with pytest.raises(IrrEvidenceError, match="SHA differs"):
        inspect_irr_evidence(FIXTURES / "bad-hash-manifest.json")
