"""Validate a bounded local threshold query input without inferring missing values."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from .manifest import load_manifest
from .util import sha256_bytes


def inspect_threshold_evidence(contract_path: str | Path) -> dict[str, object]:
    contract_file = Path(contract_path).resolve()
    root = contract_file.parents[2]
    contract = json.loads(contract_file.read_text(encoding="utf-8"))
    if contract["contracts"] != ["exact_result", "threshold_proof"]:
        raise ValueError("unknown query contracts")
    order_manifest_file = (root / contract["orders_manifest"]).resolve()
    refund_manifest_file = (root / contract["refunds_manifest"]).resolve()
    for file in (order_manifest_file, refund_manifest_file):
        file.relative_to(root)
    orders = load_manifest(order_manifest_file, strict=True)
    refunds = load_manifest(refund_manifest_file, strict=True)
    if orders.snapshot_id != refunds.snapshot_id:
        raise ValueError("snapshot mismatch")
    def present_rows(manifest, entry):
        file = (Path(manifest.source_path).parent / entry.path).resolve()
        file.relative_to(root)
        if not file.exists():
            return None
        content = file.read_bytes()
        if len(content) != entry.size or sha256_bytes(content) != entry.sha256:
            raise ValueError("file differs from trusted manifest")
        rows = list(csv.DictReader(content.decode("utf-8").splitlines()))
        if len(rows) != entry.row_count:
            raise ValueError("row count differs from trusted manifest")
        return rows
    order_rows = [row for entry in orders.files for row in (present_rows(orders, entry) or [])]
    refund_rows = [row for entry in refunds.files for row in (present_rows(refunds, entry) or [])]
    missing = [entry.path for entry in refunds.files if present_rows(refunds, entry) is None]
    if len(missing) != 1 or len(order_rows) != 4 or len(refund_rows) != 1:
        raise ValueError("unexpected bounded query fixture")
    known_net = sum(int(row["amount"]) for row in order_rows) - sum(int(row["amount"]) for row in refund_rows)
    naive_max_missing_refund = sum(int(slot["max_amount"]) for slot in refunds.stats["missing_slots"])
    return {"snapshot_id": orders.snapshot_id, "orders_manifest_sha256": sha256_bytes(order_manifest_file.read_bytes()), "refunds_manifest_sha256": sha256_bytes(refund_manifest_file.read_bytes()), "orders": order_rows, "known_refunds": refund_rows, "missing_files": missing, "missing_slots": refunds.stats["missing_slots"], "one_refund_per_order": refunds.stats["one_refund_per_order"], "refund_never_exceeds_order_amount": refunds.stats["refund_never_exceeds_order_amount"], "known_net": known_net, "naive_max_missing_refund": naive_max_missing_refund, "threshold": contract["threshold"], "contracts": contract["contracts"], "net_bounds": None, "query_decisions": None}
