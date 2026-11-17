"""Local snapshot-chain reconstruction and replication lag classification."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .manifest import Manifest, load_manifest
from .util import LakeMirrorError, parse_timestamp


class SnapshotError(LakeMirrorError):
    pass


@dataclass(frozen=True)
class SnapshotNode:
    snapshot_id: str
    parent_snapshot_id: str | None
    sequence: int
    created_at: str
    file_count: int
    total_bytes: int
    partitions: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "parent_snapshot_id": self.parent_snapshot_id,
            "sequence": self.sequence,
            "created_at": self.created_at,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "partitions": list(self.partitions),
        }


@dataclass(frozen=True)
class ChainIssue:
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class SnapshotChain:
    table: str
    side: str
    nodes: tuple[SnapshotNode, ...]
    latest_id: str | None
    pointer: str | None
    issues: tuple[ChainIssue, ...]
    broken: bool
    forked: bool

    @property
    def latest(self) -> SnapshotNode | None:
        return next((node for node in self.nodes if node.snapshot_id == self.latest_id), None)

    @property
    def ids(self) -> set[str]:
        return {node.snapshot_id for node in self.nodes}

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "side": self.side,
            "latest_id": self.latest_id,
            "pointer": self.pointer,
            "broken": self.broken,
            "forked": self.forked,
            "issues": [issue.as_dict() for issue in self.issues],
            "nodes": [node.as_dict() for node in self.nodes],
        }


@dataclass(frozen=True)
class LagFinding:
    code: str
    severity: str
    expected: Any
    actual: Any
    evidence: dict[str, Any]
    suggestion: str


def _node(manifest: Manifest) -> SnapshotNode:
    return SnapshotNode(
        manifest.snapshot_id,
        manifest.parent_snapshot_id,
        manifest.sequence,
        manifest.created_at,
        len(manifest.files),
        manifest.total_bytes(),
        tuple(sorted(manifest.partitions())),
    )


def build_chain(manifests: Iterable[Manifest], table: str, side: str, pointer: str | None) -> SnapshotChain:
    """Build a chain and detect duplicates, broken parents, forks, and regressions."""

    issues: list[ChainIssue] = []
    by_id: dict[str, SnapshotNode] = {}
    for manifest in sorted(manifests, key=lambda item: (item.sequence, item.snapshot_id)):
        node = _node(manifest)
        if node.snapshot_id in by_id:
            issues.append(ChainIssue("DUPLICATE_SNAPSHOT_ID", f"duplicate snapshot id: {node.snapshot_id}"))
            continue
        by_id[node.snapshot_id] = node
    nodes = tuple(sorted(by_id.values(), key=lambda item: (item.sequence, item.snapshot_id)))
    roots = []
    children: dict[str, list[SnapshotNode]] = {}
    for node in nodes:
        if node.parent_snapshot_id is None:
            roots.append(node)
        elif node.parent_snapshot_id not in by_id:
            issues.append(ChainIssue("SNAPSHOT_PARENT_MISSING", f"{node.snapshot_id} parent {node.parent_snapshot_id} is absent"))
        else:
            parent = by_id[node.parent_snapshot_id]
            children.setdefault(parent.snapshot_id, []).append(node)
            if node.sequence <= parent.sequence:
                issues.append(ChainIssue("SNAPSHOT_SEQUENCE_REGRESSION", f"{node.snapshot_id} sequence does not advance"))
    if len(roots) > 1:
        issues.append(ChainIssue("MULTIPLE_ROOTS", f"chain has {len(roots)} roots"))
    for parent_id, values in sorted(children.items()):
        if len(values) > 1:
            issues.append(ChainIssue("SNAPSHOT_FORK", f"parent {parent_id} has children {', '.join(sorted(item.snapshot_id for item in values))}"))
    sequences: dict[int, str] = {}
    for node in nodes:
        if node.sequence in sequences:
            issues.append(ChainIssue("DUPLICATE_SEQUENCE", f"sequence {node.sequence} shared by {sequences[node.sequence]} and {node.snapshot_id}"))
        sequences[node.sequence] = node.snapshot_id
    if pointer is not None and pointer not in by_id:
        issues.append(ChainIssue("POINTER_TARGET_MISSING", f"pointer target {pointer} is absent"))
    latest_id = max(nodes, key=lambda item: (item.sequence, item.created_at, item.snapshot_id)).snapshot_id if nodes else None
    broken = any(
        issue.code in {"DUPLICATE_SNAPSHOT_ID", "SNAPSHOT_PARENT_MISSING", "SNAPSHOT_SEQUENCE_REGRESSION", "DUPLICATE_SEQUENCE", "MULTIPLE_ROOTS", "POINTER_TARGET_MISSING"}
        for issue in issues
    )
    forked = any(issue.code in {"SNAPSHOT_FORK", "MULTIPLE_ROOTS"} for issue in issues)
    return SnapshotChain(table, side, nodes, latest_id, pointer, tuple(issues), broken, forked)


def read_pointer(table_dir: Path) -> str | None:
    path = Path(table_dir) / "_pointer.json"
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SnapshotError(f"invalid pointer JSON at {path}: {exc}", "POINTER_INVALID") from exc
    if not isinstance(value, dict) or not isinstance(value.get("snapshot_id"), str):
        raise SnapshotError(f"pointer at {path} must contain snapshot_id", "POINTER_INVALID")
    return value["snapshot_id"]


def load_chain(table_dir: Path, table: str, side: str) -> SnapshotChain:
    root = Path(table_dir)
    current = load_manifest(root / "_manifest.json")
    history_dir = root / "_snapshots"
    history = [load_manifest(path) for path in sorted(history_dir.glob("*.json"))] if history_dir.is_dir() else []
    if history:
        if current.snapshot_id not in {item.snapshot_id for item in history}:
            history.append(current)
        manifests = history
    else:
        manifests = [current]
    return build_chain(manifests, table, side, read_pointer(root))


def _latest_evidence(node: SnapshotNode | None) -> dict[str, Any]:
    return {} if node is None else node.as_dict()


def analyze_chains(primary: SnapshotChain, replica: SnapshotChain) -> list[LagFinding]:
    findings: list[LagFinding] = []
    primary_latest = primary.latest
    replica_latest = replica.latest
    primary_time = parse_timestamp(primary_latest.created_at) if primary_latest else None
    replica_time = parse_timestamp(replica_latest.created_at) if replica_latest else None
    time_delta = int((primary_time - replica_time).total_seconds()) if primary_time and replica_time else None
    evidence = {
        "primary_latest": _latest_evidence(primary_latest),
        "replica_latest": _latest_evidence(replica_latest),
        "snapshot_count_delta": len(primary.nodes) - len(replica.nodes),
        "sequence_delta": (primary_latest.sequence - replica_latest.sequence) if primary_latest and replica_latest else None,
        "time_delta_seconds": time_delta,
        "file_count_delta": (primary_latest.file_count - replica_latest.file_count) if primary_latest and replica_latest else None,
        "total_bytes_delta": (primary_latest.total_bytes - replica_latest.total_bytes) if primary_latest and replica_latest else None,
        "partition_count_delta": (len(primary_latest.partitions) - len(replica_latest.partitions)) if primary_latest and replica_latest else None,
    }
    expected = _latest_evidence(primary_latest)
    actual = _latest_evidence(replica_latest)
    if primary.broken:
        findings.append(LagFinding("SNAPSHOT_CHAIN_BROKEN", "critical", expected, actual, evidence, "Inspect the primary snapshot metadata before any repair."))
    if replica.broken:
        findings.append(LagFinding("SNAPSHOT_CHAIN_BROKEN", "critical", expected, actual, {**evidence, "issues": [item.as_dict() for item in replica.issues]}, "Do not auto-copy metadata; repair the replica chain manually."))
    if replica.forked:
        findings.append(LagFinding("SNAPSHOT_FORK", "critical", expected, actual, {**evidence, "issues": [item.as_dict() for item in replica.issues]}, "Resolve the divergent replica branch manually."))
    if replica_latest and primary_latest and replica_latest.snapshot_id not in primary.ids:
        findings.append(LagFinding("SNAPSHOT_FORK", "critical", expected, actual, evidence, "Replica latest snapshot is not present in the primary history."))
    elif primary_latest and replica_latest and replica_latest.sequence < primary_latest.sequence and not replica.broken:
        findings.append(LagFinding("SNAPSHOT_LAG", "high", expected, actual, evidence, "Replicate the missing snapshots through the controlled repair workflow."))
    if replica.latest_id != replica.pointer:
        findings.append(
            LagFinding(
                "POINTER_STALE",
                "medium",
                {"snapshot_id": replica.latest_id},
                {"snapshot_id": replica.pointer},
                evidence,
                "Update the replica pointer only after its manifest is consistent.",
            )
        )
    return findings
