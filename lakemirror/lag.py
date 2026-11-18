"""Replication lag and fork classification built from reconstructed chains."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .snapshot import SnapshotChain, SnapshotNode
from .util import parse_timestamp


@dataclass(frozen=True)
class LagFinding:
    code: str
    severity: str
    expected: Any
    actual: Any
    evidence: dict[str, Any]
    suggestion: str


def latest_evidence(node: SnapshotNode | None) -> dict[str, Any]:
    return {} if node is None else node.as_dict()


def analyze_chains(primary: SnapshotChain, replica: SnapshotChain) -> list[LagFinding]:
    findings: list[LagFinding] = []
    primary_latest = primary.latest
    replica_latest = replica.latest
    primary_time = parse_timestamp(primary_latest.created_at) if primary_latest else None
    replica_time = parse_timestamp(replica_latest.created_at) if replica_latest else None
    time_delta = int((primary_time - replica_time).total_seconds()) if primary_time and replica_time else None
    evidence = {
        "primary_latest": latest_evidence(primary_latest),
        "replica_latest": latest_evidence(replica_latest),
        "snapshot_count_delta": len(primary.nodes) - len(replica.nodes),
        "sequence_delta": (primary_latest.sequence - replica_latest.sequence) if primary_latest and replica_latest else None,
        "time_delta_seconds": time_delta,
        "file_count_delta": (primary_latest.file_count - replica_latest.file_count) if primary_latest and replica_latest else None,
        "total_bytes_delta": (primary_latest.total_bytes - replica_latest.total_bytes) if primary_latest and replica_latest else None,
        "partition_count_delta": (len(primary_latest.partitions) - len(replica_latest.partitions)) if primary_latest and replica_latest else None,
        "fork_events": [event.as_dict() for event in replica.fork_events],
        "fork_resolved": replica.fork_resolved,
    }
    expected = latest_evidence(primary_latest)
    actual = latest_evidence(replica_latest)
    if primary.broken:
        findings.append(LagFinding("SNAPSHOT_CHAIN_BROKEN", "critical", expected, actual, evidence, "Inspect the primary snapshot metadata before any repair."))
    if replica.broken:
        findings.append(LagFinding("SNAPSHOT_CHAIN_BROKEN", "critical", expected, actual, {**evidence, "issues": [item.as_dict() for item in replica.issues]}, "Do not auto-copy metadata; repair the replica chain manually."))
    if replica.forked:
        findings.append(LagFinding("SNAPSHOT_FORK", "critical", expected, actual, {**evidence, "issues": [item.as_dict() for item in replica.issues]}, "Resolve the divergent replica branch through an authorized dual-parent merge."))
    if replica_latest and primary_latest and replica_latest.snapshot_id not in primary.ids and not replica.fork_resolved:
        findings.append(LagFinding("SNAPSHOT_FORK", "critical", expected, actual, evidence, "Replica latest snapshot is not present in the primary history."))
    elif primary_latest and replica_latest and replica_latest.sequence < primary_latest.sequence and not replica.broken and not replica.forked:
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
