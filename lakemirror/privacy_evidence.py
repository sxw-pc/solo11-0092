"""Verify a complete local contingency table and two disclosure contracts.

The baseline reports trusted counts and margins but makes no cell suppression
or disclosure safety decision.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .util import safe_join, sha256_file


class PrivacyEvidenceError(ValueError):
    pass


def inspect_privacy_table(contract_path: str | Path) -> dict[str, Any]:
    manifest_file = Path(contract_path).resolve()
    contract = json.loads(manifest_file.read_text(encoding="utf-8"))
    rows = contract.get("rows")
    columns = contract.get("columns")
    if (contract.get("schema_version") != 1 or not isinstance(contract.get("snapshot_id"), str)
            or not isinstance(rows, list) or not isinstance(columns, list)
            or len(rows) != 2 or len(columns) != 3 or len(set(rows)) != 2 or len(set(columns)) != 3
            or contract.get("sensitive_if_count_below") != 2
            or contract.get("rounding_quantum") != 5
            or contract.get("protection_rule") !=
            "each_sensitive_cell_has_at_least_two_feasible_nonnegative_integer_values_given_public_artifact"):
        raise PrivacyEvidenceError("unsupported privacy table contract")
    try:
        file = safe_join(manifest_file.parent, contract["cells_file"], must_exist=True)
    except (KeyError, ValueError) as exc:
        raise PrivacyEvidenceError("cell file path invalid") from exc
    if sha256_file(file) != contract.get("cells_sha256"):
        raise PrivacyEvidenceError("cell file SHA differs from contract")
    with file.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["row", "col", "count"]:
            raise PrivacyEvidenceError("cell columns differ")
        observed = list(reader)
    if len(observed) != contract.get("row_count"):
        raise PrivacyEvidenceError("cell row count differs")
    cells = []
    seen = set()
    for cell in observed:
        key = (cell["row"], cell["col"])
        try:
            count = int(cell["count"])
        except ValueError as exc:
            raise PrivacyEvidenceError("non-integer cell count") from exc
        if (key in seen or key[0] not in rows or key[1] not in columns
                or count < 0 or str(count) != cell["count"]):
            raise PrivacyEvidenceError("duplicate or invalid cell")
        seen.add(key)
        cells.append({"row": key[0], "col": key[1], "count": count})
    if seen != {(row, col) for row in rows for col in columns}:
        raise PrivacyEvidenceError("table grid incomplete")
    row_totals = {row: sum(x["count"] for x in cells if x["row"] == row) for row in rows}
    col_totals = {col: sum(x["count"] for x in cells if x["col"] == col) for col in columns}
    awards = contract.get("publication_contracts")
    if (not isinstance(awards, list)
            or {x.get("kind") for x in awards} != {"exact_margins", "rounded_margins"}):
        raise PrivacyEvidenceError("missing publication contracts")
    return {
        "snapshot_id": contract["snapshot_id"], "parent_snapshot_id": contract.get("parent_snapshot_id"),
        "manifest_sha256": sha256_file(manifest_file),
        "cells_sha256": contract["cells_sha256"], "rows": rows, "columns": columns,
        "cells": cells, "row_totals": row_totals, "col_totals": col_totals,
        "grand_total": sum(row_totals.values()), "sensitive_if_count_below": 2,
        "rounding_quantum": 5, "protection_rule": contract["protection_rule"],
        "publication_contracts": awards,
        "feasible_sensitive_values": None, "suppression_masks": None,
        "publication_decisions": None,
    }
