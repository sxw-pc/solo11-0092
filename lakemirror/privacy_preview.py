"""Internal diagnostic of the primary-only cell suppression proposal.

This preview is deliberately not a publication API. It exposes why masking
only small cells can fail once margins or a source digest are published.
"""
from __future__ import annotations

import hashlib
from itertools import product
from typing import Any


def _round_five(value: int) -> int:
    return ((value + 2) // 5) * 5


def evaluate_candidate_mask(evidence: dict[str, Any], contract_kind: str, mask: list[str]) -> dict[str, Any]:
    """Internal diagnostic for a caller-supplied mask, not a release plan."""
    if contract_kind not in {"exact_margins", "rounded_margins"}:
        raise ValueError("unknown margin publication contract")
    rows, columns = evidence["rows"], evidence["columns"]
    keys = [(row, col) for row in rows for col in columns]
    masked = [tuple(value.split(":")) for value in mask]
    if len(set(masked)) != len(masked) or not set(masked) <= set(keys):
        raise ValueError("invalid candidate mask")
    count = {(x["row"], x["col"]): x["count"] for x in evidence["cells"]}
    sensitive = [key for key in keys if count[key] < evidence["sensitive_if_count_below"]]
    if not set(sensitive) <= set(masked):
        raise ValueError("candidate leaves sensitive cell visible")
    visible = {key: count[key] for key in keys if key not in masked}
    rounder = (lambda value: value) if contract_kind == "exact_margins" else _round_five
    row_margin = {row: rounder(evidence["row_totals"][row]) for row in rows}
    col_margin = {col: rounder(evidence["col_totals"][col]) for col in columns}
    grand_margin = rounder(evidence["grand_total"])
    possible = {key: set() for key in sensitive}
    candidate_count = 0
    scan_limit = evidence["grand_total"] + evidence["rounding_quantum"]
    for assignment in product(range(scan_limit + 1), repeat=len(masked)):
        trial = dict(visible)
        trial.update(zip(masked, assignment))
        if any(rounder(sum(trial[row, col] for col in columns)) != row_margin[row] for row in rows):
            continue
        if any(rounder(sum(trial[row, col] for row in rows)) != col_margin[col] for col in columns):
            continue
        if rounder(sum(trial.values())) != grand_margin:
            continue
        candidate_count += 1
        for key in sensitive:
            possible[key].add(trial[key])
    return {
        "internal_preview_only": True,
        "publishable": False,
        "candidate_mask": mask,
        "candidate_count": candidate_count,
        "sensitive_values": {f"{row}:{col}": sorted(possible[row, col]) for row, col in sensitive},
        "protects_sensitive_cells": all(len(values) >= 2 for values in possible.values()),
    }


def preview_primary_suppression(evidence: dict[str, Any], contract_kind: str) -> dict[str, Any]:
    if contract_kind not in {"exact_margins", "rounded_margins"}:
        raise ValueError("unknown margin publication contract")
    cells = evidence["cells"]
    rows, columns = evidence["rows"], evidence["columns"]
    threshold = evidence["sensitive_if_count_below"]
    mask = [(cell["row"], cell["col"]) for cell in cells if cell["count"] < threshold]
    visible = {(cell["row"], cell["col"]): cell["count"] for cell in cells if (cell["row"], cell["col"]) not in mask}
    rounder = (lambda value: value) if contract_kind == "exact_margins" else _round_five
    public_rows = {row: rounder(evidence["row_totals"][row]) for row in rows}
    public_cols = {col: rounder(evidence["col_totals"][col]) for col in columns}
    public_grand = rounder(evidence["grand_total"])
    candidate_values = {key: set() for key in mask}
    hash_matches = []
    scan_limit = evidence["grand_total"] + evidence["rounding_quantum"]
    for assignment in product(range(scan_limit + 1), repeat=len(mask)):
        trial = dict(visible)
        trial.update(zip(mask, assignment))
        if (any(rounder(sum(trial[(row, col)] for col in columns)) != public_rows[row] for row in rows)
                or any(rounder(sum(trial[(row, col)] for row in rows)) != public_cols[col] for col in columns)
                or rounder(sum(trial.values())) != public_grand):
            continue
        for key, value in zip(mask, assignment):
            candidate_values[key].add(value)
        content = "row,col,count\n" + "".join(
            f'{cell["row"]},{cell["col"]},{trial[(cell["row"], cell["col"])]}\n' for cell in cells)
        if hashlib.sha256(content.encode("utf-8")).hexdigest() == evidence["cells_sha256"]:
            hash_matches.append(dict(zip(mask, assignment)))
    return {
        "internal_preview_only": True,
        "snapshot_id": evidence["snapshot_id"],
        "contract_kind": contract_kind,
        "primary_mask": [f"{row}:{col}" for row, col in mask],
        "candidate_scan_limit": scan_limit,
        "candidate_values": {f"{row}:{col}": sorted(values) for (row, col), values in candidate_values.items()},
        "uniquely_recovered_by_margins": [f"{row}:{col}" for (row, col), values in candidate_values.items() if len(values) == 1],
        "source_sha_dictionary_matches": len(hash_matches),
        "would_leak_if_source_sha_published": len(hash_matches) == 1,
        "publishable": False,
    }
