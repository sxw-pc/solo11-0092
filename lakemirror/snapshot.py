"""Local snapshot-chain reconstruction and replication lag classification."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .manifest import Manifest, load_manifest
from .util import LakeMirrorError


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
    parent_ids: tuple[str, ...] = ()
    merge_contract: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "parent_snapshot_id": self.parent_snapshot_id,
            "sequence": self.sequence,
            "created_at": self.created_at,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "partitions": list(self.partitions),
            "parent_ids": list(self.parent_ids),
            "merge_contract": self.merge_contract,
        }


@dataclass(frozen=True)
class ChainIssue:
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class ForkEvent:
    """A fork that happened in history and the merge that resolved it, if any."""

    ancestor_id: str
    branch_ids: tuple[str, ...]
    resolved_by: str | None
    resolved_contract: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ancestor_id": self.ancestor_id,
            "branch_ids": list(self.branch_ids),
            "resolved_by": self.resolved_by,
            "resolved_contract": self.resolved_contract,
        }


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
    fork_events: tuple[ForkEvent, ...] = ()
    fork_resolved: bool = False
    merge_count: int = 0

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
            "fork_resolved": self.fork_resolved,
            "merge_count": self.merge_count,
            "issues": [issue.as_dict() for issue in self.issues],
            "fork_events": [event.as_dict() for event in self.fork_events],
            "nodes": [node.as_dict() for node in self.nodes],
        }




_BROKEN_CODES = {
    "DUPLICATE_SNAPSHOT_ID",
    "SNAPSHOT_PARENT_MISSING",
    "SNAPSHOT_SEQUENCE_REGRESSION",
    "DUPLICATE_SEQUENCE",
    "MULTIPLE_ROOTS",
    "POINTER_TARGET_MISSING",
    "MERGE_PARENT_INVALID",
    "MERGE_LINEAGE_INVALID",
    "MERGE_SEQUENCE_REGRESSION",
}


def _node(manifest: Manifest) -> SnapshotNode:
    return SnapshotNode(
        manifest.snapshot_id,
        manifest.parent_snapshot_id,
        manifest.sequence,
        manifest.created_at,
        len(manifest.files),
        manifest.total_bytes(),
        tuple(sorted(manifest.partitions())),
        manifest.parent_ids(),
        manifest.merge.contract if manifest.merge is not None else None,
    )


def _merge_issues(manifest: Manifest, by_id: dict[str, SnapshotNode]) -> list[ChainIssue]:
    merge = manifest.merge
    if merge is None:
        return []
    issues: list[ChainIssue] = []
    refs = (merge.ancestor, *merge.parents)
    for ref in refs:
        if ref.snapshot_id not in by_id:
            issues.append(ChainIssue("MERGE_PARENT_INVALID", f"{manifest.snapshot_id} references missing snapshot {ref.snapshot_id}"))
            continue
        node = by_id[ref.snapshot_id]
        if manifest.sequence <= node.sequence:
            issues.append(ChainIssue("MERGE_SEQUENCE_REGRESSION", f"{manifest.snapshot_id} sequence does not advance over {ref.snapshot_id}"))
    parent_ids = [ref.snapshot_id for ref in merge.parents]
    if not issues and (manifest.parent_snapshot_id != parent_ids[0] or len(set(parent_ids)) != 2):
        issues.append(ChainIssue("MERGE_PARENT_INVALID", f"{manifest.snapshot_id} merge parents are not two distinct branches"))
    merged_partitions = {entry.partition for entry in manifest.files}
    for ref in merge.parents:
        if ref.snapshot_id not in by_id:
            continue
        if not ref.files or {entry.partition for entry in ref.files} != merged_partitions:
            issues.append(ChainIssue("MERGE_LINEAGE_INVALID", f"{manifest.snapshot_id} parent {ref.snapshot_id} evidence does not cover merged partitions"))
    return issues


def build_chain(manifests: Iterable[Manifest], table: str, side: str, pointer: str | None) -> SnapshotChain:
    """Build a chain and detect duplicates, broken parents, forks, merges, and regressions."""

    issues: list[ChainIssue] = []
    by_id: dict[str, SnapshotNode] = {}
    by_manifest: dict[str, Manifest] = {}
    for manifest in sorted(manifests, key=lambda item: (item.sequence, item.snapshot_id)):
        node = _node(manifest)
        if node.snapshot_id in by_id:
            issues.append(ChainIssue("DUPLICATE_SNAPSHOT_ID", f"duplicate snapshot id: {node.snapshot_id}"))
            continue
        by_id[node.snapshot_id] = node
        by_manifest[node.snapshot_id] = manifest
    nodes = tuple(sorted(by_id.values(), key=lambda item: (item.sequence, item.snapshot_id)))
    roots: list[SnapshotNode] = []
    children: dict[str, list[SnapshotNode]] = {}
    for node in nodes:
        if not node.parent_ids:
            roots.append(node)
            continue
        for parent_id in node.parent_ids:
            if parent_id not in by_id:
                issues.append(ChainIssue("SNAPSHOT_PARENT_MISSING", f"{node.snapshot_id} parent {parent_id} is absent"))
                continue
            parent = by_id[parent_id]
            children.setdefault(parent.snapshot_id, []).append(node)
            if node.sequence <= parent.sequence:
                issues.append(ChainIssue("SNAPSHOT_SEQUENCE_REGRESSION", f"{node.snapshot_id} sequence does not advance"))
        if node.merge_contract is not None:
            issues.extend(_merge_issues(by_manifest[node.snapshot_id], by_id))
    if len(roots) > 1:
        issues.append(ChainIssue("MULTIPLE_ROOTS", f"chain has {len(roots)} roots"))
    for parent_id, values in sorted(children.items()):
        unique = sorted({item.snapshot_id for item in values})
        if len(unique) > 1:
            issues.append(ChainIssue("SNAPSHOT_FORK", f"parent {parent_id} has children {', '.join(unique)}"))
    sequences: dict[int, str] = {}
    for node in nodes:
        if node.sequence in sequences:
            issues.append(ChainIssue("DUPLICATE_SEQUENCE", f"sequence {node.sequence} shared by {sequences[node.sequence]} and {node.snapshot_id}"))
        sequences[node.sequence] = node.snapshot_id
    if pointer is not None and pointer not in by_id:
        issues.append(ChainIssue("POINTER_TARGET_MISSING", f"pointer target {pointer} is absent"))
    latest_id = max(nodes, key=lambda item: (item.sequence, item.created_at, item.snapshot_id)).snapshot_id if nodes else None
    merge_nodes = {node.snapshot_id: node for node in nodes if node.merge_contract is not None}
    fork_events = _fork_events(children, merge_nodes)
    unresolved_fork = any(event.resolved_by is None for event in fork_events) or len(roots) > 1
    all_resolved = bool(fork_events) and all(event.resolved_by is not None for event in fork_events)
    broken = any(issue.code in _BROKEN_CODES for issue in issues)
    return SnapshotChain(
        table,
        side,
        nodes,
        latest_id,
        pointer,
        tuple(issues),
        broken,
        forked=unresolved_fork,
        fork_events=tuple(fork_events),
        fork_resolved=all_resolved and not unresolved_fork,
        merge_count=len(merge_nodes),
    )


def _fork_events(children: dict[str, list[SnapshotNode]], merge_nodes: dict[str, SnapshotNode]) -> list[ForkEvent]:
    events: list[ForkEvent] = []
    for ancestor_id, values in sorted(children.items()):
        branch_ids = tuple(sorted({item.snapshot_id for item in values}))
        if len(branch_ids) < 2:
            continue
        branch_set = set(branch_ids)
        resolver: SnapshotNode | None = None
        for node in sorted(merge_nodes.values(), key=lambda item: item.sequence):
            if set(node.parent_ids) == branch_set:
                resolver = node
                break
        events.append(
            ForkEvent(
                ancestor_id,
                branch_ids,
                resolver.snapshot_id if resolver is not None else None,
                resolver.merge_contract if resolver is not None else None,
            )
        )
    return events


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


