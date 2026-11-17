"""Verify an all-bytes-present local cash-flow lake partition.

The baseline binds a project snapshot to its CSV and appraisal contracts but
does not compute NPV roots or authorize an investment.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .util import safe_join, sha256_file


class IrrEvidenceError(ValueError):
    pass


def inspect_irr_evidence(manifest_path: str | Path) -> dict[str, Any]:
    manifest_file = Path(manifest_path).resolve()
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if (manifest.get("schema_version") != 1 or manifest.get("period_unit") != "year"
            or manifest.get("amount_unit") != "cents"
            or manifest.get("discount_convention") != "annual_effective"
            or not isinstance(manifest.get("project_id"), str)
            or not isinstance(manifest.get("snapshot_id"), str)):
        raise IrrEvidenceError("unsupported project cash-flow manifest")
    try:
        csv_file = safe_join(manifest_file.parent, manifest["cashflow_file"], must_exist=True)
    except (KeyError, ValueError) as exc:
        raise IrrEvidenceError("cash-flow file path invalid") from exc
    if sha256_file(csv_file) != manifest.get("cashflow_sha256"):
        raise IrrEvidenceError("cash-flow SHA differs from manifest")
    with csv_file.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["period", "amount_cents"]:
            raise IrrEvidenceError("cash-flow columns differ")
        records = list(reader)
    if len(records) != manifest.get("row_count") or len(records) < 3:
        raise IrrEvidenceError("cash-flow row count differs")
    cashflows = []
    for expected_period, row in enumerate(records):
        if set(row) != {"period", "amount_cents"} or not all(isinstance(v, str) for v in row.values()):
            raise IrrEvidenceError("invalid cash-flow row")
        try:
            period, amount = int(row["period"]), int(row["amount_cents"])
        except ValueError as exc:
            raise IrrEvidenceError("non-integer cash-flow row") from exc
        if period != expected_period or str(period) != row["period"] or str(amount) != row["amount_cents"]:
            raise IrrEvidenceError("cash-flow periods or amounts are not canonical")
        cashflows.append({"period": period, "amount_cents": amount})
    interval = manifest.get("admissible_rate_bps")
    hurdle = manifest.get("hurdle_bps")
    if (not isinstance(interval, list) or len(interval) != 2
            or any(type(x) is not int for x in interval) or not 0 <= interval[0] < interval[1] <= 10000
            or type(hurdle) is not int or not interval[0] < hurdle < interval[1]):
        raise IrrEvidenceError("invalid appraisal rate interval")
    contracts = manifest.get("investment_contracts")
    if (not isinstance(contracts, list)
            or {x.get("kind") for x in contracts} != {"hurdle_npv", "unique_irr"}):
        raise IrrEvidenceError("missing appraisal contracts")
    return {
        "project_id": manifest["project_id"], "snapshot_id": manifest["snapshot_id"],
        "parent_snapshot_id": manifest.get("parent_snapshot_id"),
        "manifest_sha256": sha256_file(manifest_file),
        "cashflow_file": manifest["cashflow_file"],
        "cashflow_sha256": manifest["cashflow_sha256"],
        "cashflows": cashflows, "admissible_rate_bps": interval, "hurdle_bps": hurdle,
        "investment_contracts": contracts,
        "npv_at_hurdle": None, "admissible_irr_roots": None,
        "investment_decisions": None,
    }
