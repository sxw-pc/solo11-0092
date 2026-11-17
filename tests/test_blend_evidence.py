from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from lakemirror.blend_evidence import BlendEvidenceError, inspect_blend_assay
from lakemirror.blend_preview import preview_blend

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "blend_assay"


def test_shared_bias_changes_two_valid_input_recipes() -> None:
    evidence = inspect_blend_assay(FIXTURE / "manifest.json")
    assert [(x["lot"], x["cost_cents"], x["bias_slope"]) for x in evidence["lots"]] == [
        ("A", "100", "0"), ("B", "200", "1"), ("C", "220", "-1")]
    nominal = preview_blend(evidence, {"A": "2/3", "B": "0", "C": "1/3"})
    assert (nominal["cost_cents"], nominal["nominal_potency"], nominal["potency_at_bias_maximum"]) == (
        "140", "6", "16/3")
    robust_witness = preview_blend(evidence, {"A": "3/5", "B": "1/5", "C": "1/5"})
    assert (robust_witness["cost_cents"], robust_witness["potency_at_bias_minimum"],
            robust_witness["potency_at_bias_maximum"]) == ("144", "6", "6")
    assert robust_witness["independent_per_lot_lower_potency"] == "26/5"
    assert robust_witness["publishable"] is False
    assert evidence["minimum_cost_recipe"] is evidence["budget_decisions"] is None


def test_bad_sha_and_path_escape_block_assay(tmp_path: Path) -> None:
    shutil.copytree(FIXTURE, tmp_path / "blend")
    root = tmp_path / "blend"
    (root / "assay.csv").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(BlendEvidenceError, match="SHA"):
        inspect_blend_assay(root / "manifest.json")
    shutil.copyfile(FIXTURE / "assay.csv", root / "assay.csv")
    data = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    data["assay_file"] = "../assay.csv"
    (root / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(BlendEvidenceError, match="invalid"):
        inspect_blend_assay(root / "manifest.json")
