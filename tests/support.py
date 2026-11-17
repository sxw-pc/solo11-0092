"""Shared temporary-workspace helpers for the offline test suite."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from lakemirror.config import Config, load_config
from lakemirror.fixtures import generate_fixture
from lakemirror.manifest import Manifest, parse_manifest

ALL_CODES = [
    "DUPLICATE_LOGICAL_PATH",
    "EMPTY_TABLE",
    "FILE_EXTRA_IN_REPLICA",
    "FILE_MISSING_IN_REPLICA",
    "FILE_SHA256_MISMATCH",
    "FILE_SIZE_MISMATCH",
    "INVALID_PARTITION_VALUE",
    "PARTITION_COLUMN_MISSING",
    "PARTITION_EXTRA",
    "PARTITION_MISSING",
    "PARTITION_SPEC_MISMATCH",
    "PATH_OUTSIDE_ROOT",
    "POINTER_STALE",
    "ROW_COUNT_MISMATCH",
    "SCHEMA_ADDITIVE_OK",
    "SCHEMA_FIELD_MISSING",
    "SCHEMA_FIELD_ORDER_MISMATCH",
    "SCHEMA_NULLABILITY_MISMATCH",
    "SCHEMA_TYPE_MISMATCH",
    "SNAPSHOT_CHAIN_BROKEN",
    "SNAPSHOT_FORK",
    "SNAPSHOT_LAG",
]
LENIENT_AUTO = [
    "FILE_MISSING_IN_REPLICA",
    "FILE_EXTRA_IN_REPLICA",
    "FILE_SIZE_MISMATCH",
    "FILE_SHA256_MISMATCH",
    "ROW_COUNT_MISMATCH",
    "PARTITION_MISSING",
    "PARTITION_EXTRA",
    "POINTER_STALE",
]
STRICT_AUTO = [
    "FILE_MISSING_IN_REPLICA",
    "FILE_EXTRA_IN_REPLICA",
    "FILE_SIZE_MISMATCH",
    "PARTITION_MISSING",
    "PARTITION_EXTRA",
    "POINTER_STALE",
]
TABLE_SPECS = [
    {"name": "events", "path": "tables/events", "business_key": "event_id", "partition_columns": ["day"]},
    {"name": "orders", "path": "tables/orders", "business_key": "order_id", "partition_columns": ["dt"]},
]


def workspace(tmp: str | Path, scenario: str = "drift_basic", seed: int = 42) -> Path:
    work = Path(tmp).resolve() / "work"
    generate_fixture(scenario, seed, work)
    return work


def config_data(work: Path, mode: str = "lenient", *, ignore_rules: list[dict[str, Any]] | None = None,
                max_operations: int = 200, allow_delete: bool = True,
                checks: dict[str, bool] | None = None, tables: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    auto = LENIENT_AUTO if mode == "lenient" else STRICT_AUTO
    default_checks = {
        "file_size": True,
        "sha256": True,
        "row_count": True,
        "partition_coverage": True,
        "snapshot_chain": True,
        "schema_compat": True,
        "partition_spec": True,
    }
    default_checks.update(checks or {})
    return {
        "schema_version": 1,
        "primary_root": str(work / "lake" / "primary"),
        "replica_root": str(work / "lake" / "replica"),
        "workdir": str(work),
        "ledger": str(work / "ledger.db"),
        "tables": copy.deepcopy(tables if tables is not None else TABLE_SPECS),
        "checks": default_checks,
        "repair": {
            "mode": mode,
            "auto_fix": auto,
            "manual_review": [code for code in ALL_CODES if code not in auto],
            "safety": {
                "allow_delete": allow_delete,
                "max_operations": max_operations,
                "backup_dir": "backups",
                "dry_run_default": True,
            },
        },
        "ignore_rules": ignore_rules or [],
    }


def write_config(tmp: str | Path, data: dict[str, Any], name: str = "config.json") -> Path:
    path = Path(tmp).resolve() / name
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def make_config(tmp: str | Path, scenario: str = "drift_basic", mode: str = "lenient", **kwargs: Any) -> Config:
    work = workspace(tmp, scenario)
    path = write_config(tmp, config_data(work, mode, **kwargs))
    return load_config(path)


SCHEMA = [
    {"name": "id", "type": "LONG", "nullable": False},
    {"name": "dt", "type": "STRING", "nullable": False},
]


def file_entry(path: str = "data/dt=2024-01-01/a.csv", partition: str = "dt=2024-01-01",
               size: int = 1, row_count: int = 1, sha: str = "0" * 64) -> dict[str, Any]:
    return {"path": path, "partition": partition, "size": size, "row_count": row_count, "sha256": sha}


def manifest_doc(snapshot_id: str = "snap-1", parent: str | None = None, sequence: int = 1,
                 table: str = "probe", files: list[dict[str, Any]] | None = None,
                 schema: list[dict[str, Any]] | None = None, spec: list[str] | None = None) -> dict[str, Any]:
    return {
        "table": table,
        "snapshot_id": snapshot_id,
        "parent_snapshot_id": parent,
        "sequence": sequence,
        "created_at": f"2024-01-{sequence:02d}T00:00:00Z",
        "schema": schema or SCHEMA,
        "partition_spec": spec or ["dt"],
        "files": files if files is not None else [file_entry()],
        "stats": {},
    }


def manifest(**kwargs: Any) -> Manifest:
    return parse_manifest(manifest_doc(**kwargs))


def scan_plan(config: Config, scan_id: str = "scan-test") -> tuple[Any, dict[str, Any]]:
    from lakemirror.planner import build_plan
    from lakemirror.reconcile import scan_config

    result = scan_config(config, scan_id)
    plan = build_plan(config, result.scan_id, [item.as_dict() for item in result.diffs])
    return result, plan
