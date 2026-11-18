"""Prove bounded threshold predicates without inventing bytes for missing files.

Every present file is verified against its trust-anchored manifest (path, size,
SHA-256, row count, and declared CSV schema).  Missing files contribute no rows;
their impact is modelled only through the trusted ``missing_slots`` metadata.
Two authorization contracts are evaluated independently on the same verified
input: ``exact_result`` refuses to answer while referenced bytes are absent,
while ``threshold_proof`` reasons over *all* legal completions of the missing
slots and exposes only a predicate, rigorous bounds, and a worst-case witness.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from .manifest import Manifest, load_manifest
from .threshold_proof import (
    missing_slots,
    order_values,
    refund_values,
    reject,
    slot_domains,
    worst_case,
)
from .util import LakeMirrorError, safe_join, sha256_bytes

_SCHEMAS: dict[str, tuple[str, ...]] = {
    "orders": ("order_id", "amount"),
    "refunds": ("refund_id", "order_id", "amount"),
}
_CONTRACT_FIELDS = {"orders_manifest", "refunds_manifest", "threshold", "contracts"}


def _load_contract(path: Path) -> dict[str, Any]:
    if not path.is_file():
        reject(f"contract does not exist: {path}", "THRESHOLD_CONTRACT")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        reject(f"invalid contract: {exc}", "THRESHOLD_CONTRACT")
    if not isinstance(data, dict) or set(data) != _CONTRACT_FIELDS:
        reject("contract must contain exactly the declared query fields", "THRESHOLD_CONTRACT")
    if data["contracts"] != ["exact_result", "threshold_proof"]:
        reject("unknown query contracts", "THRESHOLD_CONTRACT")
    if type(data["threshold"]) is not int:
        reject("threshold must be an integer", "THRESHOLD_CONTRACT")
    for key in ("orders_manifest", "refunds_manifest"):
        if not isinstance(data[key], str) or not data[key]:
            reject(f"{key} must be a relative path", "THRESHOLD_CONTRACT")
    return data


def _resolve_input(root: Path, contract_dir: Path, relative: str) -> Path:
    """Resolve a contract path near the contract, falling back to the fixed root."""

    try:
        local = safe_join(contract_dir, relative)
        shared = safe_join(root, relative)
    except LakeMirrorError as exc:
        reject(f"unsafe contract path {relative!r}: {exc}", "THRESHOLD_PATH")
    local_exists, shared_exists = local.is_file(), shared.is_file()
    if local_exists and shared_exists and local.resolve() != shared.resolve():
        reject(f"ambiguous contract path: {relative}", "THRESHOLD_CONTRACT")
    if local_exists:
        chosen = local
    elif shared_exists:
        chosen = shared
    else:
        reject(f"input does not exist: {relative}", "THRESHOLD_MISSING")
    try:
        chosen.resolve().relative_to(root)
    except ValueError:
        reject(f"input escapes the query root: {relative}", "THRESHOLD_PATH")
    return chosen


def _load_source(root: Path, contract_dir: Path, relative: str, table: str) -> tuple[Manifest, str, str]:
    path = _resolve_input(root, contract_dir, relative)
    manifest = load_manifest(path, strict=True)
    if manifest.table != table:
        reject(f"{relative} is not a {table} manifest", "THRESHOLD_IDENTITY")
    if tuple(field.name for field in manifest.fields) != _SCHEMAS[table]:
        reject(f"{relative} schema does not match the bounded query", "THRESHOLD_IDENTITY")
    return manifest, sha256_bytes(path.read_bytes()), relative


def _verify_entry(root: Path, manifest: Manifest, entry: Any) -> dict[str, Any] | None:
    try:
        path = safe_join(Path(manifest.source_path).parent, entry.path)
    except LakeMirrorError as exc:
        reject(f"unsafe file path {entry.path!r}: {exc}", "THRESHOLD_PATH")
    if not path.exists():
        return None
    if not path.is_file():
        reject(f"referenced path is not a regular file: {entry.path}", "THRESHOLD_FILE")
    content = path.read_bytes()
    if len(content) != entry.size:
        reject(f"size differs from trusted manifest: {entry.path}", "THRESHOLD_FILE")
    digest = sha256_bytes(content)
    if digest != entry.sha256:
        reject(f"sha256 differs from trusted manifest: {entry.path}", "THRESHOLD_FILE")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        reject(f"file is not UTF-8: {entry.path}: {exc}", "THRESHOLD_FILE")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    fields = _SCHEMAS[manifest.table]
    if tuple(reader.fieldnames or ()) != fields:
        reject(f"CSV header differs from manifest schema: {entry.path}", "THRESHOLD_ROWS")
    rows: list[dict[str, str]] = []
    for raw in reader:
        if None in raw or any(raw.get(name) is None for name in fields):
            reject(f"CSV row shape is invalid: {entry.path}", "THRESHOLD_ROWS")
        rows.append({name: raw[name] for name in fields})
    if len(rows) != entry.row_count:
        reject(f"row_count differs from trusted manifest: {entry.path}", "THRESHOLD_ROWS")
    return {
        "table": manifest.table,
        "path": entry.path,
        "sha256": digest,
        "size": len(content),
        "row_count": len(rows),
        "rows": rows,
    }


def _collect(
    root: Path, orders: Manifest, refunds: Manifest
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]], list[dict[str, str]]]:
    verified: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    order_rows: list[dict[str, str]] = []
    refund_rows: list[dict[str, str]] = []
    for manifest, target in ((orders, order_rows), (refunds, refund_rows)):
        for entry in manifest.files:
            checked = _verify_entry(root, manifest, entry)
            if checked is None:
                missing.append({
                    "table": manifest.table,
                    "path": entry.path,
                    "size": entry.size,
                    "row_count": entry.row_count,
                    "declared_sha256": entry.sha256,
                    "bytes_present": False,
                })
                continue
            target.extend(checked["rows"])
            verified.append({key: checked[key] for key in ("table", "path", "sha256", "size", "row_count")})
    return verified, missing, order_rows, refund_rows


def inspect_threshold_evidence(contract_path: str | Path) -> dict[str, Any]:
    """Verify query inputs and return both contracts' independent decisions."""

    contract_file = Path(contract_path).resolve()
    if len(contract_file.parents) < 3:
        reject("contract must live under a query root", "THRESHOLD_CONTRACT")
    root = contract_file.parents[2]
    contract = _load_contract(contract_file)
    orders, orders_sha, orders_rel = _load_source(root, contract_file.parent, contract["orders_manifest"], "orders")
    refunds, refunds_sha, refunds_rel = _load_source(root, contract_file.parent, contract["refunds_manifest"], "refunds")
    if orders.snapshot_id != refunds.snapshot_id:
        reject("orders and refunds manifests belong to different snapshots", "THRESHOLD_SNAPSHOT")
    verified, missing, order_rows, refund_rows = _collect(root, orders, refunds)
    order_amounts, orders_out = order_values(order_rows)
    known_ids, refunded_orders, refunds_out = refund_values(refund_rows, order_amounts)
    slots = missing_slots(refunds.stats, missing, order_amounts, known_ids, refunded_orders)
    known_net = sum(order_amounts.values()) - sum(row["amount"] for row in refunds_out)
    naive_max = sum(slot["max_amount"] for slot in slots)
    if slots:
        max_refund, witness = worst_case(slots, order_amounts)
        lower, upper, exact = known_net - max_refund, known_net, None
    else:
        max_refund, witness = 0, []
        lower = upper = exact = known_net
    if not lower <= upper:
        reject("derived net bounds are inconsistent", "THRESHOLD_CONSTRAINT")
    if exact is not None and not lower <= exact <= upper:
        reject("verified exact value falls outside the allowed range", "THRESHOLD_CONSTRAINT")

    threshold = contract["threshold"]
    predicate = f"net_revenue >= {threshold}"
    if missing:
        exact_decision: dict[str, Any] = {
            "contract": "exact_result",
            "decision": "unknown",
            "consumption_allowed": False,
            "net": None,
            "reason": "REFERENCED_FILE_BYTES_MISSING",
            "missing_files": [item["path"] for item in missing],
        }
    else:
        exact_decision = {
            "contract": "exact_result",
            "decision": "exact",
            "consumption_allowed": True,
            "net": exact,
            "reason": "ALL_REFERENCED_FILES_VERIFIED",
            "within_computed_bounds": lower <= exact <= upper,
        }
    if exact is not None:
        decision = "true" if exact >= threshold else "false"
        proof_reason = "EXACT_VALUE_MEETS_THRESHOLD" if decision == "true" else "EXACT_VALUE_BELOW_THRESHOLD"
    elif lower >= threshold:
        decision, proof_reason = "true", "ALL_LEGAL_COMPLETIONS_MEET_THRESHOLD"
    elif upper < threshold:
        decision, proof_reason = "false", "ALL_LEGAL_COMPLETIONS_BELOW_THRESHOLD"
    else:
        decision, proof_reason = "unknown", "THRESHOLD_NOT_DECIDABLE"
    proof_decision = {
        "contract": "threshold_proof",
        "decision": decision,
        "consumption_allowed": decision == "true",
        "predicate": predicate,
        "reason": proof_reason,
        "lower_bound": lower,
        "upper_bound": upper,
        "worst_case_completion": witness,
    }
    missing_identity = [
        {
            "table": item["table"],
            "path": item["path"],
            "size": item["size"],
            "row_count": item["row_count"],
            "declared_sha256": item["declared_sha256"],
            "bytes_present": False,
            "slot_count": sum(1 for slot in slots if slot["file"] == item["path"]),
        }
        for item in missing
    ]
    return {
        "schema_version": 1,
        "snapshot_id": orders.snapshot_id,
        "manifests": {
            "orders": {"path": orders_rel, "sha256": orders_sha},
            "refunds": {"path": refunds_rel, "sha256": refunds_sha},
        },
        "contracts": contract["contracts"],
        "threshold": threshold,
        "predicate": predicate,
        "verified_files": verified,
        "missing_files": [item["path"] for item in missing],
        "missing_file_identities": missing_identity,
        "orders": orders_out,
        "known_refunds": refunds_out,
        "known_net": known_net,
        "naive_max_missing_refund": naive_max,
        "max_feasible_missing_refund": max_refund,
        "missing_slots": slots,
        "completion_space": {
            "verified_order_count": len(orders_out),
            "verified_refund_count": len(refunds_out),
            "missing_slot_count": len(slots),
            "slot_domains": slot_domains(slots, order_amounts),
            "max_total_missing_refund": max_refund,
            "constraint_source": f"{refunds_rel}#/stats",
            "constraint_sources": [
                f"{refunds_rel}#/stats/missing_slots",
                f"{refunds_rel}#/stats/one_refund_per_order",
                f"{refunds_rel}#/stats/refund_never_exceeds_order_amount",
            ],
        },
        "constraints": {
            "source": f"{refunds_rel}#/stats",
            "one_refund_per_order": True,
            "refund_never_exceeds_order_amount": True,
            "non_negative_amounts": True,
            "known_refunded_orders": sorted(refunded_orders),
        },
        "net_bounds": {
            "lower": lower,
            "upper": upper,
            "exact": exact,
            "lower_witness": witness,
            "upper_basis": "non_negative_missing_refund_amounts" if missing else "verified_rows",
        },
        "worst_case_completion": witness,
        "query_decisions": {
            "exact_result": exact_decision,
            "threshold_proof": proof_decision,
        },
    }


def query_threshold_evidence(contract_path: str | Path) -> dict[str, Any]:
    """Public query entry point for both authorization contracts."""

    return inspect_threshold_evidence(contract_path)
