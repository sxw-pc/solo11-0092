"""Verify a local assay table and its shared calibration contract."""
from __future__ import annotations

import csv
import json
from fractions import Fraction
from pathlib import Path
from typing import Any

from .util import PathSecurityError, safe_join, sha256_file


class BlendEvidenceError(ValueError):
    pass


def inspect_blend_assay(manifest_path: str | Path) -> dict[str, Any]:
    file = Path(manifest_path).resolve()
    try:
        manifest = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BlendEvidenceError("invalid blend manifest") from exc
    try:
        if (manifest.get("schema_version") != 1 or not isinstance(manifest.get("snapshot_id"), str)
                or manifest.get("batch_total") != "1" or manifest.get("potency_minimum") != "6"
                or manifest.get("budget_cents") != "142"
                or manifest.get("shared_calibration_bias") != {"minimum": "-2", "maximum": "2"}
                or manifest.get("contracts") != ["nominal_calibration", "all_calibrations"]):
            raise BlendEvidenceError("unsupported blend contract")
        assay = safe_join(file.parent, manifest["assay_file"], must_exist=True)
        if sha256_file(assay) != manifest["assay_sha256"]:
            raise BlendEvidenceError("assay SHA differs")
        with assay.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != ["lot", "cost_cents", "base_potency", "bias_slope"]:
                raise BlendEvidenceError("assay columns differ")
            rows = list(reader)
        if len(rows) != 3 or {row["lot"] for row in rows} != {"A", "B", "C"}:
            raise BlendEvidenceError("assay lot identities differ")
        lots = []
        for row in rows:
            cost = Fraction(row["cost_cents"])
            base = Fraction(row["base_potency"])
            slope = Fraction(row["bias_slope"])
            if cost <= 0 or base <= 0:
                raise BlendEvidenceError("assay values invalid")
            lots.append({"lot": row["lot"], "cost_cents": str(cost),
                         "base_potency": str(base), "bias_slope": str(slope)})
    except (KeyError, TypeError, ValueError, ZeroDivisionError, PathSecurityError) as exc:
        if isinstance(exc, BlendEvidenceError):
            raise
        raise BlendEvidenceError("invalid blend evidence") from exc
    return {
        "snapshot_id": manifest["snapshot_id"],
        "manifest_sha256": sha256_file(file),
        "assay_sha256": manifest["assay_sha256"],
        "lots": lots,
        "batch_total": "1",
        "potency_minimum": "6",
        "budget_cents": "142",
        "bias_minimum": "-2",
        "bias_maximum": "2",
        "contracts": manifest["contracts"],
        "minimum_cost_recipe": None,
        "budget_decisions": None,
    }
