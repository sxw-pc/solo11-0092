"""Configuration value objects shared by the loader and consumers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .util import LakeMirrorError


class ConfigError(LakeMirrorError):
    def __init__(self, message: str, field: str = "") -> None:
        super().__init__(f"config error [{field or 'config'}]: {message}", "CONFIG_INVALID")
        self.field = field


@dataclass(frozen=True)
class TableConfig:
    name: str
    path: str
    business_key: str
    partition_columns: tuple[str, ...]


@dataclass(frozen=True)
class Checks:
    file_size: bool = True
    sha256: bool = True
    row_count: bool = True
    partition_coverage: bool = True
    snapshot_chain: bool = True
    schema_compat: bool = True
    partition_spec: bool = True


@dataclass(frozen=True)
class RepairPolicy:
    mode: str
    auto_fix: frozenset[str]
    manual_review: frozenset[str]
    allow_delete: bool
    max_operations: int
    backup_dir: Path


@dataclass(frozen=True)
class IgnoreRule:
    code: str
    table: str
    path_prefix: str
    reason: str


@dataclass(frozen=True)
class Config:
    config_path: Path
    primary_root: Path
    replica_root: Path
    workdir: Path
    ledger_path: Path
    tables: dict[str, TableConfig]
    checks: Checks
    repair: RepairPolicy
    ignore_rules: tuple[IgnoreRule, ...]
    dry_run_default: bool
    fingerprint: str

    def table(self, name: str) -> TableConfig:
        try:
            return self.tables[name]
        except KeyError as exc:
            raise ConfigError(f"unknown table {name!r}", "tables") from exc

    def can_auto(self, code: str) -> bool:
        if code not in self.repair.auto_fix:
            return False
        if self.repair.mode == "strict" and code in {
            "FILE_SHA256_MISMATCH",
            "ROW_COUNT_MISMATCH",
        }:
            return False
        return True

    def snapshot(self) -> dict[str, Any]:
        return {
            "primary_root": str(self.primary_root),
            "replica_root": str(self.replica_root),
            "workdir": str(self.workdir),
            "ledger_path": str(self.ledger_path),
            "dry_run_default": self.dry_run_default,
            "tables": [
                {
                    "name": table.name,
                    "path": table.path,
                    "business_key": table.business_key,
                    "partition_columns": list(table.partition_columns),
                }
                for table in self.tables.values()
            ],
            "checks": vars(self.checks),
            "repair": {
                "mode": self.repair.mode,
                "auto_fix": sorted(self.repair.auto_fix),
                "manual_review": sorted(self.repair.manual_review),
                "allow_delete": self.repair.allow_delete,
                "max_operations": self.repair.max_operations,
                "backup_dir": str(self.repair.backup_dir),
            },
            "ignore_rules": [vars(rule) for rule in self.ignore_rules],
        }
