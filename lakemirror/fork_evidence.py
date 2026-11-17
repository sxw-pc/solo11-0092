"""Inspect local forked snapshot row evidence without merging or publishing it."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from .manifest import load_manifest
from .snapshot import build_chain
from .util import sha256_bytes


def inspect_fork_contract(contract_path: str | Path) -> dict[str, object]:
    contract_file = Path(contract_path).resolve()
    root = contract_file.parents[2]
    contract = json.loads(contract_file.read_text(encoding="utf-8"))
    if contract["contracts"] != ["file_strict", "field_disjoint"]:
        raise ValueError("unknown fork contracts")
    manifests = []
    rows_by_snapshot = {}
    evidence = []
    for role in ("ancestor", "primary", "replica"):
        manifest_file = (root / contract[role]).resolve()
        manifest_file.relative_to(root)
        manifest = load_manifest(manifest_file, strict=True)
        if manifest.table != "orders" or len(manifest.files) != 1:
            raise ValueError("unexpected fork manifest")
        entry = manifest.files[0]
        file = (manifest_file.parent / entry.path).resolve()
        file.relative_to(root)
        content = file.read_bytes()
        if len(content) != entry.size or sha256_bytes(content) != entry.sha256:
            raise ValueError("fork file differs from manifest")
        rows = list(csv.DictReader(content.decode("utf-8").splitlines()))
        if len(rows) != entry.row_count or len({r["order_id"] for r in rows}) != len(rows):
            raise ValueError("invalid row identity or count")
        manifests.append(manifest)
        rows_by_snapshot[manifest.snapshot_id] = {r["order_id"]: r for r in rows}
        evidence.append({"role": role, "snapshot_id": manifest.snapshot_id, "parent_snapshot_id": manifest.parent_snapshot_id, "manifest_sha256": sha256_bytes(manifest_file.read_bytes()), "file_sha256": entry.sha256, "rows": rows})
    ancestor, primary, replica = manifests
    if primary.parent_snapshot_id != ancestor.snapshot_id or replica.parent_snapshot_id != ancestor.snapshot_id:
        raise ValueError("snapshots are not sibling branches")
    chain = build_chain(manifests, "orders", "combined", None)
    return {"contracts": contract["contracts"], "expected_merge_parents": contract["expected_merge_parents"], "chain_forked": chain.forked, "chain_issues": [x.as_dict() for x in chain.issues], "evidence": evidence, "rows_by_snapshot": rows_by_snapshot, "merge_verdict": None, "merged_snapshot": None}
