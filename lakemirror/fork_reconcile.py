"""Readable-versus-blocked verdict for a sandboxed fork merge head."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .snapshot import SnapshotChain, load_chain


def reconcile_fork_head(table_dir: str | Path, table: str) -> dict[str, Any]:
    """Decide whether a fork-merged table head may be treated as authoritative."""

    root = Path(table_dir).resolve()
    chain: SnapshotChain = load_chain(root, table, "merged")
    blocking_issues = [
        issue.as_dict()
        for issue in chain.issues
        if (issue.code.startswith("SNAPSHOT_") and issue.code != "SNAPSHOT_FORK")
        or issue.code.startswith("MERGE_")
    ]
    fork_issues = [issue.as_dict() for issue in chain.issues if issue.code == "SNAPSHOT_FORK"]
    pointer_ok = chain.pointer is not None and chain.pointer == chain.latest_id
    readable = (
        not chain.broken
        and not chain.forked
        and chain.fork_resolved
        and chain.merge_count >= 1
        and pointer_ok
        and not blocking_issues
    )
    reasons: list[str] = []
    if chain.broken:
        reasons.append("snapshot chain is broken")
    if chain.forked:
        reasons.append("unresolved fork remains in history")
    if not chain.fork_resolved and not chain.forked:
        reasons.append("no dual-parent merge resolution recorded")
    if not pointer_ok:
        reasons.append("pointer does not target the verified merge head")
    if blocking_issues:
        reasons.append("blocking chain issues present")
    return {
        "table": table,
        "table_dir": str(root),
        "head_snapshot_id": chain.latest_id,
        "pointer_snapshot_id": chain.pointer,
        "forked": chain.forked,
        "fork_resolved": chain.fork_resolved,
        "merge_count": chain.merge_count,
        "fork_events": [event.as_dict() for event in chain.fork_events],
        "fork_issues": fork_issues,
        "blocking_issues": blocking_issues,
        "decision": "readable" if readable else "blocked",
        "reasons": reasons,
        "chain": chain.as_dict(),
    }
