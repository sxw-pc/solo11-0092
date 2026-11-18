"""Inspect local forked snapshot row evidence and evaluate authorized merge contracts."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .fork_merge import CONTRACTS, MergeOutcome, evaluate_contract
from .manifest import Manifest, load_manifest, manifest_digest
from .snapshot import build_chain
from .util import LakeMirrorError, sha256_bytes

ROLES = ("ancestor", "primary", "replica")


def _resolve_root(contract_file: Path, contract: dict[str, Any]) -> Path:
    """Find the project root from which every contract-relative path resolves."""

    for candidate in (contract_file.parent, *contract_file.parents):
        if all((candidate / str(contract.get(role, ""))).is_file() for role in ROLES):
            return candidate
    raise LakeMirrorError("contract manifest paths do not resolve from any parent directory", "FORK_MANIFEST_MISSING")


@dataclass(frozen=True)
class ForkBundle:
    root: Path
    contract_path: Path
    contract: dict[str, Any]
    manifests: dict[str, Manifest]
    manifest_paths: dict[str, Path]
    file_bytes: dict[str, bytes]
    rows_by_snapshot: dict[str, dict[str, dict[str, str]]]
    evidence: list[dict[str, Any]]


def load_fork_bundle(contract_path: str | Path) -> ForkBundle:
    """Load and verify a fork contract against manifest SHA/size/row_count facts."""

    contract_file = Path(contract_path).resolve()
    contract = json.loads(contract_file.read_text(encoding="utf-8"))
    if contract.get("contracts") != list(CONTRACTS):
        raise LakeMirrorError("unknown fork contracts", "FORK_CONTRACT_UNKNOWN")
    root = _resolve_root(contract_file, contract)
    expected_parents = contract.get("expected_merge_parents")
    if not isinstance(expected_parents, list) or len(expected_parents) != 2:
        raise LakeMirrorError("expected_merge_parents must name two snapshots", "FORK_CONTRACT_INVALID")
    manifests: dict[str, Manifest] = {}
    manifest_paths: dict[str, Path] = {}
    file_bytes: dict[str, bytes] = {}
    rows_by_snapshot: dict[str, dict[str, dict[str, str]]] = {}
    evidence: list[dict[str, Any]] = []
    for role in ROLES:
        raw_rel = contract.get(role)
        if not isinstance(raw_rel, str):
            raise LakeMirrorError(f"contract role {role} path missing", "FORK_CONTRACT_INVALID")
        manifest_file = (root / raw_rel).resolve()
        manifest_file.relative_to(root)
        if not manifest_file.is_file():
            raise LakeMirrorError(f"manifest not found for {role}: {raw_rel}", "FORK_MANIFEST_MISSING")
        manifest = load_manifest(manifest_file, strict=True)
        if manifest.table != "orders" or len(manifest.files) != 1:
            raise LakeMirrorError("unexpected fork manifest shape", "FORK_MANIFEST_SHAPE")
        entry = manifest.files[0]
        data_file = (manifest_file.parent / entry.path).resolve()
        data_file.relative_to(root)
        if not data_file.is_file():
            raise LakeMirrorError(f"data file missing: {entry.path}", "FORK_FILE_MISSING")
        content = data_file.read_bytes()
        actual_sha = sha256_bytes(content)
        if len(content) != entry.size or actual_sha != entry.sha256:
            raise LakeMirrorError(f"file facts diverge from manifest for {entry.path}", "FORK_FILE_HASH_MISMATCH")
        manifest_sha = manifest_digest(manifest)
        from .fork_merge import read_csv_rows

        headers, rows = read_csv_rows(content)
        if len(rows) != entry.row_count:
            raise LakeMirrorError(f"row count mismatch for {entry.path}", "FORK_ROW_COUNT_MISMATCH")
        key = "order_id"
        if key not in headers or len({row[key] for row in rows}) != len(rows):
            raise LakeMirrorError(f"invalid or duplicate business key in {entry.path}", "FORK_KEY_INVALID")
        manifests[role] = manifest
        manifest_paths[role] = manifest_file
        file_bytes[f"{role}:{entry.path}"] = content
        rows_by_snapshot[manifest.snapshot_id] = {row[key]: row for row in rows}
        evidence.append(
            {
                "role": role,
                "snapshot_id": manifest.snapshot_id,
                "parent_snapshot_id": manifest.parent_snapshot_id,
                "manifest_path": str(manifest_file.relative_to(root)),
                "manifest_sha256": manifest_sha,
                "file_path": entry.path,
                "file_sha256": entry.sha256,
                "size": entry.size,
                "row_count": entry.row_count,
                "headers": headers,
                "rows": rows,
            }
        )
    ancestor, primary, replica = (manifests[role] for role in ROLES)
    if primary.parent_snapshot_id != ancestor.snapshot_id or replica.parent_snapshot_id != ancestor.snapshot_id:
        raise LakeMirrorError("snapshots are not sibling branches of one ancestor", "FORK_NOT_SIBLINGS")
    if [primary.snapshot_id, replica.snapshot_id] != expected_parents:
        raise LakeMirrorError("expected_merge_parents do not match the sibling snapshots", "FORK_CONTRACT_INVALID")
    return ForkBundle(root, contract_file, contract, manifests, manifest_paths, file_bytes, rows_by_snapshot, evidence)


def evaluate_bundle(bundle: ForkBundle, key: str = "order_id") -> dict[str, MergeOutcome]:
    ancestor, primary, replica = (bundle.manifests[role] for role in ROLES)
    outcomes: dict[str, MergeOutcome] = {}
    for name in CONTRACTS:
        outcomes[name] = evaluate_contract(name, key, ancestor, primary, replica, bundle.file_bytes)
    return outcomes


def inspect_fork_contract(contract_path: str | Path) -> dict[str, Any]:
    bundle = load_fork_bundle(contract_path)
    chain = build_chain(list(bundle.manifests.values()), "orders", "combined", None)
    outcomes = evaluate_bundle(bundle)
    return {
        "contracts": list(CONTRACTS),
        "expected_merge_parents": bundle.contract["expected_merge_parents"],
        "chain_forked": chain.forked,
        "chain_fork_resolved": chain.fork_resolved,
        "chain_issues": [item.as_dict() for item in chain.issues],
        "fork_events": [item.as_dict() for item in chain.fork_events],
        "evidence": bundle.evidence,
        "rows_by_snapshot": bundle.rows_by_snapshot,
        "merge_verdicts": {name: outcome.as_dict() for name, outcome in outcomes.items()},
        "merge_verdict": None,
        "merged_snapshot": None,
    }
