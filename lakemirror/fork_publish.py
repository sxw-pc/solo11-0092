"""Atomic, replay-safe publication of authoritative dual-parent merge snapshots.

Write order is crash-safe: merged CSV bytes, then the merge manifest with
dual-parent evidence, then the current-head manifest, and only last the
pointer. Every step uses atomic temp-file replacement, and a recorded plan
fingerprint makes retries idempotent while rejecting a second, different
snapshot under the same id.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .fork_evidence import ForkBundle, load_fork_bundle
from .fork_merge import CONTRACTS, MergeOutcome, evaluate_contract, render_csv
from .manifest import (
    FileEntry,
    Manifest,
    MergeLineage,
    MergeRef,
    canonical_manifest_doc,
    load_manifest,
    manifest_canonical_text,
    manifest_digest,
)
from .snapshot import build_chain, read_pointer
from .util import LakeMirrorError, atomic_write_bytes, fingerprint, sha256_bytes, utc_now, within


class ForkPublishError(LakeMirrorError):
    pass


@dataclass(frozen=True)
class PublishPlan:
    snapshot_id: str
    contract: str
    sequence: int
    csv_path: str
    csv_bytes: bytes
    manifest: Manifest
    basis: list[Any]

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.basis)


def _lineage(bundle: ForkBundle, contract: str, manifest: Manifest) -> MergeLineage:
    refs: list[MergeRef] = []
    for role in ("primary", "replica"):
        source = bundle.manifests[role]
        refs.append(MergeRef(role, source.snapshot_id, manifest_digest(source), source.files))
    ancestor = bundle.manifests["ancestor"]
    return MergeLineage(
        contract,
        MergeRef("ancestor", ancestor.snapshot_id, manifest_digest(ancestor), ancestor.files),
        tuple(refs),
    )


def build_publish_plan(bundle: ForkBundle, contract: str, snapshot_id: str, key: str = "order_id", created_at: str | None = None) -> PublishPlan:
    if contract not in CONTRACTS:
        raise ForkPublishError(f"unknown merge contract: {contract}", "FORK_CONTRACT_UNKNOWN")
    outcome: MergeOutcome = evaluate_contract(
        contract,
        key,
        bundle.manifests["ancestor"],
        bundle.manifests["primary"],
        bundle.manifests["replica"],
        bundle.file_bytes,
    )
    if not outcome.allowed:
        raise ForkPublishError(f"contract {contract} blocks publication", "FORK_MERGE_BLOCKED")
    ancestor = bundle.manifests["ancestor"]
    primary = bundle.manifests["primary"]
    replica = bundle.manifests["replica"]
    if ancestor.fields != primary.fields or ancestor.fields != replica.fields or ancestor.partition_spec != primary.partition_spec or ancestor.partition_spec != replica.partition_spec:
        raise ForkPublishError("branch schema or partition spec drifted", "FORK_SCHEMA_DRIFT")
    template = ancestor.files[0]
    csv_path = f"orders-{snapshot_id}.csv"
    content = render_csv(outcome.headers, outcome.rows)
    entry = FileEntry(csv_path, template.partition, len(content), len(outcome.rows), sha256_bytes(content))
    sequence = max(item.sequence for item in bundle.manifests.values()) + 1
    draft = Manifest(
        table=ancestor.table,
        snapshot_id=snapshot_id,
        parent_snapshot_id=primary.snapshot_id,
        sequence=sequence,
        created_at=created_at or utc_now(),
        fields=ancestor.fields,
        partition_spec=ancestor.partition_spec,
        files=(entry,),
        stats={"file_count": 1, "total_bytes": len(content)},
    )
    manifest = Manifest(**{**draft.__dict__, "merge": _lineage(bundle, contract, draft)})
    basis = [
        contract,
        snapshot_id,
        ancestor.snapshot_id,
        [primary.snapshot_id, replica.snapshot_id],
        outcome.headers,
        outcome.rows,
        [[item.key, item.column, item.source] for item in outcome.provenance],
        entry.as_dict(),
        manifest.created_at,
    ]
    return PublishPlan(snapshot_id, contract, sequence, csv_path, content, manifest, basis)


def _table_dir(sandbox: Path, table: str) -> Path:
    root = Path(sandbox).resolve()
    table_dir = root / table
    if not within(root, table_dir):
        raise ForkPublishError("sandbox table path escapes the sandbox root", "FORK_PATH_UNSAFE")
    return table_dir


def _plan_path(table_dir: Path, snapshot_id: str) -> Path:
    return table_dir / "_merge_plans" / f"{snapshot_id}.json"


def _existing_created_at(table_dir: Path, snapshot_id: str) -> str | None:
    snapshot_path = table_dir / "_snapshots" / f"{snapshot_id}.json"
    if snapshot_path.exists():
        return load_manifest(snapshot_path).created_at
    plan_path = _plan_path(table_dir, snapshot_id)
    if plan_path.exists():
        record = json.loads(plan_path.read_text(encoding="utf-8"))
        value = record.get("created_at")
        return value if isinstance(value, str) else None
    return None


def _write_plan_if_absent(table_dir: Path, plan: PublishPlan) -> Path:
    path = _plan_path(table_dir, plan.snapshot_id)
    record = {
        "snapshot_id": plan.snapshot_id,
        "contract": plan.contract,
        "fingerprint": plan.fingerprint,
        "csv_path": plan.csv_path,
        "csv_sha256": plan.manifest.files[0].sha256,
        "created_at": plan.manifest.created_at,
    }
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        if previous.get("fingerprint") != plan.fingerprint:
            raise ForkPublishError(f"a different merge plan already exists for {plan.snapshot_id}", "FORK_PLAN_CONFLICT")
        return path
    atomic_write_bytes(path, json.dumps(record, sort_keys=True, indent=2).encode("utf-8") + b"\n")
    return path


def _verify_chain(table_dir: Path, table: str, snapshot_id: str) -> dict[str, Any]:
    history_dir = table_dir / "_snapshots"
    manifests = [load_manifest(item) for item in sorted(history_dir.glob("*.json"))]
    chain = build_chain(manifests, table, "combined", read_pointer(table_dir))
    if chain.forked or not chain.fork_resolved or chain.merge_count != 1:
        raise ForkPublishError("published chain did not resolve the fork", "FORK_VERIFY_FAILED")
    if chain.latest_id != snapshot_id or read_pointer(table_dir) != snapshot_id:
        raise ForkPublishError("pointer does not target the verified merge head", "FORK_VERIFY_FAILED")
    return chain.as_dict()


def publish_fork_merge(
    contract_path: str | Path,
    contract: str,
    sandbox: str | Path,
    *,
    snapshot_id: str = "S3",
    execute: bool = False,
) -> dict[str, Any]:
    bundle = load_fork_bundle(contract_path)
    sandbox_root = _table_dir(Path(sandbox), bundle.manifests["ancestor"].table)
    created_at = _existing_created_at(sandbox_root, snapshot_id)
    try:
        plan = build_publish_plan(bundle, contract, snapshot_id, created_at=created_at)
    except ForkPublishError as exc:
        if exc.code == "FORK_MERGE_BLOCKED":
            outcome = evaluate_contract(
                contract,
                "order_id",
                bundle.manifests["ancestor"],
                bundle.manifests["primary"],
                bundle.manifests["replica"],
                bundle.file_bytes,
            )
            return {"status": "blocked", "contract": contract, "snapshot_id": snapshot_id,
                    "blocks": [item.as_dict() for item in outcome.blocks], "fingerprint": None, "verification": None}
        raise
    table_dir = _table_dir(Path(sandbox), plan.manifest.table)
    planned = {
        "status": "planned" if not execute else "publishing",
        "contract": contract,
        "snapshot_id": snapshot_id,
        "sequence": plan.sequence,
        "parents": list(plan.manifest.parent_ids()),
        "ancestor": bundle.manifests["ancestor"].snapshot_id,
        "csv": plan.manifest.files[0].as_dict(),
        "manifest_sha256": manifest_digest(plan.manifest),
        "fingerprint": plan.fingerprint,
    }
    if not execute:
        planned["would_write"] = [
            str(table_dir / plan.csv_path),
            str(table_dir / "_snapshots" / f"{snapshot_id}.json"),
            str(table_dir / "_manifest.json"),
            str(table_dir / "_pointer.json"),
        ]
        return planned
    history_dir = table_dir / "_snapshots"
    history_dir.mkdir(parents=True, exist_ok=True)
    _write_plan_if_absent(table_dir, plan)
    manifest_path = history_dir / f"{snapshot_id}.json"
    if manifest_path.exists():
        existing = load_manifest(manifest_path)
        if manifest_digest(existing) != manifest_digest(plan.manifest):
            raise ForkPublishError(f"a different snapshot already uses id {snapshot_id}", "FORK_SNAPSHOT_CONFLICT")
        status = "already_published"
    else:
        for role in ("ancestor", "primary", "replica"):
            source_manifest = bundle.manifests[role]
            target = history_dir / f"{source_manifest.snapshot_id}.json"
            if not target.exists():
                atomic_write_bytes(target, manifest_canonical_text(source_manifest).encode("utf-8"))
        atomic_write_bytes(table_dir / plan.csv_path, plan.csv_bytes)
        reread = (table_dir / plan.csv_path).read_bytes()
        if sha256_bytes(reread) != plan.manifest.files[0].sha256:
            raise ForkPublishError("merged CSV bytes failed read-back verification", "FORK_VERIFY_FAILED")
        atomic_write_bytes(manifest_path, manifest_canonical_text(plan.manifest).encode("utf-8"))
        loaded = load_manifest(manifest_path)
        if manifest_digest(loaded) != manifest_digest(plan.manifest):
            raise ForkPublishError("merge manifest failed read-back verification", "FORK_VERIFY_FAILED")
        atomic_write_bytes(table_dir / "_manifest.json", manifest_canonical_text(plan.manifest).encode("utf-8"))
        pointer = {"table": plan.manifest.table, "snapshot_id": snapshot_id, "manifest_sha256": manifest_digest(plan.manifest)}
        atomic_write_bytes(table_dir / "_pointer.json", (json.dumps(pointer, sort_keys=True, indent=2) + "\n").encode("utf-8"))
        status = "published"
    chain_report = _verify_chain(table_dir, plan.manifest.table, snapshot_id)
    return {**planned, "status": status, "verification": {
        "chain": chain_report,
        "pointer": read_pointer(table_dir),
        "rows_reread": plan.manifest.files[0].row_count,
        "csv_sha256": plan.manifest.files[0].sha256,
        "lineage": canonical_manifest_doc(plan.manifest)["merge"],
    }}
