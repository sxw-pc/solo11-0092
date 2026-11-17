"""Value objects for reconciliation results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config
    from .snapshot import SnapshotChain


@dataclass(frozen=True)
class Diff:
    diff_id: str
    code: str
    table: str
    partition: str
    path: str
    expected: Any
    actual: Any
    severity: str
    suggestion: str
    auto_fixable: bool
    ignored: bool = False
    ignore_reason: str = ""
    evidence: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "diff_id": self.diff_id,
            "code": self.code,
            "table": self.table,
            "partition": self.partition,
            "path": self.path,
            "expected": self.expected,
            "actual": self.actual,
            "severity": self.severity,
            "suggestion": self.suggestion,
            "auto_fixable": self.auto_fixable,
            "ignored": self.ignored,
            "ignore_reason": self.ignore_reason,
            "evidence": self.evidence or {},
        }


@dataclass(frozen=True)
class ScanResult:
    scan_id: str
    config: "Config"
    diffs: tuple[Diff, ...]
    chains: tuple["SnapshotChain", ...]
    table_stats: dict[str, dict[str, Any]]
    stats: dict[str, Any]
    warnings: tuple[str, ...]

    @property
    def active_diffs(self) -> list[Diff]:
        return [item for item in self.diffs if not item.ignored]

    @property
    def auto_diffs(self) -> list[Diff]:
        return [item for item in self.active_diffs if item.auto_fixable]
