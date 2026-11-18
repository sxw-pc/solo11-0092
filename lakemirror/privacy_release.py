"""Turn a verified contingency table into a contract-authorized safe release.

The internal preview deliberately shows why masking only small cells fails.
This module replaces that preview with an exhaustive release decision: it
selects a minimum-size suppression set, proves that every truly sensitive cell
keeps at least two feasible non-negative integer values under the final public
artifact, and separates a public artifact from an internal audit receipt.
"""
from __future__ import annotations

from itertools import combinations, product
from pathlib import Path
from typing import Any

from .util import LakeMirrorError, canonical_json, fingerprint, stable_id, write_json

PUBLIC_KIND = "privacy_table_public_artifact"
RECEIPT_KIND = "privacy_table_internal_receipt"
CONTRACT_META = {
    "exact_margins": {"publish_margins": "exact", "rounding_quantum": 5},
    "rounded_margins": {"publish_margins": "nearest_five", "rounding_quantum": 5},
}
PUBLIC_KEYS = frozenset({
    "schema_version", "kind", "publishable", "publication_id", "rows", "columns",
    "visible_cells", "margins", "contract",
})


class PrivacyReleaseError(LakeMirrorError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="PRIVACY_RELEASE_BLOCKED")


def _round_five(value: int) -> int:
    return ((value + 2) // 5) * 5


def _display_total(value: int, contract_kind: str) -> int:
    return value if contract_kind == "exact_margins" else _round_five(value)


def _rounding_interval(margin: int) -> range:
    """Non-negative integers whose nearest-five rounding equals ``margin``."""
    return range(max(0, margin - 2), margin + 3)


def _authorized_contract(evidence: dict[str, Any], contract_kind: str) -> dict[str, Any]:
    if contract_kind not in CONTRACT_META:
        raise PrivacyReleaseError("unknown margin publication contract")
    entries = [item for item in evidence.get("publication_contracts") or []
               if isinstance(item, dict) and item.get("kind") == contract_kind]
    meta = CONTRACT_META[contract_kind]
    if len(entries) != 1:
        raise PrivacyReleaseError("publication contract is not pre-authorized")
    entry = entries[0]
    if (entry.get("publish_margins") != meta["publish_margins"]
            or entry.get("objective") != "minimum_suppressed_cells"
            or evidence.get("rounding_quantum") != meta["rounding_quantum"]):
        raise PrivacyReleaseError("publication contract terms are unsupported")
    return entry


def _fill(cells: list[tuple[str, str]], row_need: dict[str, int],
          col_need: dict[str, int]) -> list[dict[tuple[str, str], int]]:
    """Enumerate non-negative integer assignments to ``cells`` honoring margins."""
    results: list[dict[tuple[str, str], int]] = []

    def walk(index: int, left_rows: dict[str, int], left_cols: dict[str, int],
             chosen: list[int]) -> None:
        if index == len(cells):
            if all(value == 0 for value in left_rows.values()) and all(
                    value == 0 for value in left_cols.values()):
                results.append(dict(zip(cells, chosen)))
            return
        row, col = cells[index]
        for value in range(min(left_rows[row], left_cols[col]) + 1):
            next_rows = dict(left_rows)
            next_cols = dict(left_cols)
            next_rows[row] -= value
            next_cols[col] -= value
            chosen.append(value)
            walk(index + 1, next_rows, next_cols, chosen)
            chosen.pop()

    walk(0, dict(row_need), dict(col_need), [])
    return results


def _completions(evidence: dict[str, Any], contract_kind: str,
                 mask: tuple[tuple[str, str], ...]) -> list[dict[tuple[str, str], int]]:
    """All non-negative completions matching the public visible cells and margins."""
    rows, columns = evidence["rows"], evidence["columns"]
    keys = [(row, col) for row in rows for col in columns]
    count = {(cell["row"], cell["col"]): cell["count"] for cell in evidence["cells"]}
    hidden = set(mask)
    visible = {key: count[key] for key in keys if key not in hidden}
    visible_rows = {row: sum(count[(row, col)] for col in columns if (row, col) not in hidden)
                    for row in rows}
    visible_cols = {col: sum(count[(row, col)] for row in rows if (row, col) not in hidden)
                    for col in columns}
    row_margin = {row: _display_total(evidence["row_totals"][row], contract_kind) for row in rows}
    col_margin = {col: _display_total(evidence["col_totals"][col], contract_kind) for col in columns}
    grand_margin = _display_total(evidence["grand_total"], contract_kind)
    if contract_kind == "exact_margins":
        row_options = [[row_margin[row]] for row in rows]
        col_options = [[col_margin[col]] for col in columns]
        grand_options = [grand_margin]
    else:
        row_options = [list(_rounding_interval(row_margin[row])) for row in rows]
        col_options = [list(_rounding_interval(col_margin[col])) for col in columns]
        grand_options = list(_rounding_interval(grand_margin))
    cells = [key for key in keys if key in hidden]
    completions: list[dict[tuple[str, str], int]] = []
    for row_targets in product(*row_options):
        row_need = {row: row_targets[index] - visible_rows[row] for index, row in enumerate(rows)}
        if any(value < 0 for value in row_need.values()):
            continue
        for col_targets in product(*col_options):
            total = sum(row_targets)
            if total != sum(col_targets) or total not in grand_options:
                continue
            col_need = {col: col_targets[index] - visible_cols[col]
                        for index, col in enumerate(columns)}
            if any(value < 0 for value in col_need.values()):
                continue
            for filled in _fill(cells, row_need, col_need):
                completions.append({**visible, **filled})
    return completions


def _analyse(evidence: dict[str, Any], contract_kind: str,
             mask: tuple[tuple[str, str], ...], sensitive: list[tuple[str, str]]) -> tuple[
                 list[dict[tuple[str, str], int]], dict[tuple[str, str], list[int]], bool]:
    completions = _completions(evidence, contract_kind, mask)
    values = {cell: sorted({completion[cell] for completion in completions})
              for cell in sensitive}
    return completions, values, all(len(options) >= 2 for options in values.values())


def _select_mask(evidence: dict[str, Any], contract_kind: str) -> dict[str, Any]:
    rows, columns = evidence["rows"], evidence["columns"]
    keys = [(row, col) for row in rows for col in columns]
    index = {key: position for position, key in enumerate(keys)}
    count = {(cell["row"], cell["col"]): cell["count"] for cell in evidence["cells"]}
    threshold = evidence["sensitive_if_count_below"]
    sensitive = [key for key in keys if count[key] < threshold]
    others = [key for key in keys if key not in set(sensitive)]
    evaluated = []
    for size in range(len(sensitive), len(keys) + 1):
        protecting = []
        for extra in combinations(others, size - len(sensitive)):
            mask = tuple(sorted(sensitive + list(extra), key=index.get))
            completions, values, safe = _analyse(evidence, contract_kind, mask, sensitive)
            if safe:
                protecting.append((mask, completions, values))
        evaluated.append({"suppressed_cells": size, "protecting_masks": len(protecting)})
        if protecting:
            protecting.sort(key=lambda item: [index[cell] for cell in item[0]])
            mask, completions, values = protecting[0]
            return {"mask": mask, "completions": completions, "values": values,
                    "sensitive": sensitive, "evaluated": evaluated,
                    "protecting_masks": len(protecting)}
    raise PrivacyReleaseError("no suppression plan protects every sensitive cell")


def _witnesses(sensitive: list[tuple[str, str]],
               completions: list[dict[tuple[str, str], int]]) -> dict[str, list[dict[str, int]]]:
    """Two complete feasible tables per sensitive cell differing on that cell."""
    result: dict[str, list[dict[str, int]]] = {}
    for cell in sensitive:
        chosen: list[dict[tuple[str, str], int]] = []
        for completion in completions:
            if all(completion[cell] != prior[cell] for prior in chosen):
                chosen.append(completion)
            if len(chosen) == 2:
                break
        result[f"{cell[0]}:{cell[1]}"] = [
            {f"{row}:{col}": value for (row, col), value in completion.items()}
            for completion in chosen
        ]
    return result


def build_privacy_release(evidence: dict[str, Any], contract_kind: str) -> dict[str, Any]:
    """Return ``public_artifact`` plus a private ``internal_receipt``."""
    contract = _authorized_contract(evidence, contract_kind)
    selection = _select_mask(evidence, contract_kind)
    mask = selection["mask"]
    hidden = set(mask)
    rows, columns = evidence["rows"], evidence["columns"]
    keys = [(row, col) for row in rows for col in columns]
    count = {(cell["row"], cell["col"]): cell["count"] for cell in evidence["cells"]}
    visible_cells = [{"row": row, "col": col, "count": count[(row, col)]}
                     for row, col in keys if (row, col) not in hidden]
    public_without_id = {
        "schema_version": 1,
        "kind": PUBLIC_KIND,
        "publishable": True,
        "rows": list(rows),
        "columns": list(columns),
        "visible_cells": visible_cells,
        "margins": {
            "row_totals": {row: _display_total(evidence["row_totals"][row], contract_kind)
                           for row in rows},
            "column_totals": {col: _display_total(evidence["col_totals"][col], contract_kind)
                              for col in columns},
            "grand_total": _display_total(evidence["grand_total"], contract_kind),
        },
        "contract": {
            "kind": contract_kind,
            "publish_margins": contract["publish_margins"],
            "objective": contract["objective"],
            "rounding_quantum": CONTRACT_META[contract_kind]["rounding_quantum"],
            "sensitive_if_count_below": evidence["sensitive_if_count_below"],
            "protection_rule": evidence["protection_rule"],
        },
    }
    public = dict(public_without_id)
    public["publication_id"] = stable_id("privacy-release", public_without_id)
    sensitive = selection["sensitive"]
    receipt = {
        "schema_version": 1,
        "kind": RECEIPT_KIND,
        "internal_only": True,
        "publishable": False,
        "publication_id": public["publication_id"],
        "artifact_fingerprint": fingerprint(public),
        "snapshot_id": evidence["snapshot_id"],
        "parent_snapshot_id": evidence.get("parent_snapshot_id"),
        "manifest_sha256": evidence["manifest_sha256"],
        "source_cells_sha256": evidence["cells_sha256"],
        "contract": dict(public["contract"]),
        "contract_kind": contract_kind,
        "true_cells": [{"row": row, "col": col, "count": count[(row, col)]}
                       for row, col in keys],
        "suppressed_cells": [f"{row}:{col}" for row, col in mask],
        "sensitive_cells": [f"{row}:{col}" for row, col in sensitive],
        "feasible_sensitive_values": {f"{row}:{col}": selection["values"][(row, col)]
                                      for row, col in sensitive},
        "witnesses": _witnesses(sensitive, selection["completions"]),
        "optimality": {
            "objective": contract["objective"],
            "exhaustive": True,
            "search": "ascending suppressed-cell count over all subsets of non-sensitive cells",
            "tie_break": "lexicographically smallest row-major cell set",
            "evaluated_sizes": selection["evaluated"],
            "minimum_suppressed_cells": len(mask),
            "protecting_masks_at_minimum": selection["protecting_masks"],
        },
    }
    return {"public_artifact": public, "internal_receipt": receipt}


def write_release_bundle(release: dict[str, Any], out_dir: str | Path) -> dict[str, str]:
    """Write the public artifact and receipt as two separate local files."""
    out_dir = Path(out_dir)
    written = {}
    for name, key in (("public-artifact.json", "public_artifact"),
                      ("internal-receipt.json", "internal_receipt")):
        path = Path(out_dir) / name
        write_json(path, release[key])
        written[key] = str(path.resolve())
    return written


def verify_release_receipt(evidence: dict[str, Any], contract_kind: str,
                           public_artifact: dict[str, Any],
                           receipt: dict[str, Any]) -> dict[str, Any]:
    """Re-bind a receipt to one artifact, contract, and verified snapshot."""
    if not isinstance(receipt, dict) or receipt.get("kind") != RECEIPT_KIND:
        raise PrivacyReleaseError("receipt identity is invalid")
    if receipt.get("contract_kind") != contract_kind:
        raise PrivacyReleaseError("receipt contract differs; contracts cannot be cross-reused")
    if receipt.get("snapshot_id") != evidence["snapshot_id"]:
        raise PrivacyReleaseError("receipt snapshot differs; stale receipts are rejected")
    if receipt.get("source_cells_sha256") != evidence["cells_sha256"]:
        raise PrivacyReleaseError("receipt source digest differs; stale receipts are rejected")
    if receipt.get("manifest_sha256") != evidence["manifest_sha256"]:
        raise PrivacyReleaseError("receipt snapshot identity differs; stale receipts are rejected")
    if not isinstance(public_artifact, dict) or public_artifact.get("kind") != PUBLIC_KIND:
        raise PrivacyReleaseError("public artifact identity is invalid")
    if set(public_artifact) - PUBLIC_KEYS:
        raise PrivacyReleaseError("public artifact carries fields outside the release whitelist")
    contract_field = public_artifact.get("contract")
    if not isinstance(contract_field, dict) or contract_field.get("kind") != contract_kind:
        raise PrivacyReleaseError("artifact contract differs; contracts cannot be cross-reused")
    if fingerprint(public_artifact) != receipt.get("artifact_fingerprint"):
        raise PrivacyReleaseError("public artifact fingerprint differs from receipt")
    rebuilt = build_privacy_release(evidence, contract_kind)
    if canonical_json(rebuilt["public_artifact"]) != canonical_json(public_artifact):
        raise PrivacyReleaseError("public artifact is not the authorized release for this evidence")
    if canonical_json(rebuilt["internal_receipt"]) != canonical_json(receipt):
        raise PrivacyReleaseError("receipt fingerprint differs from the authorized decision")
    return {"verified": True, "publication_id": public_artifact["publication_id"],
            "snapshot_id": evidence["snapshot_id"], "contract_kind": contract_kind}
