"""Sandboxed plan execution with preflight checks, backups, and idempotency."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .ledger import Ledger
from .plan_io import validate_plan
from .rollback import rollback_batch
from .util import (
    LakeMirrorError,
    atomic_copy,
    atomic_write_text,
    file_facts,
    pretty_json,
    safe_join,
    utc_now,
    within,
)


class ExecutionError(LakeMirrorError):
    def __init__(self, message: str, code: str = "EXECUTION_REFUSED") -> None:
        super().__init__(message, code)


@dataclass
class ExecutionResult:
    batch_id: str
    status: str
    dry_run: bool
    executed: int = 0
    skipped: int = 0
    manual: int = 0
    failed: int = 0
    operations: list[dict[str, Any]] = field(default_factory=list)
    backup_dir: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "status": self.status,
            "dry_run": self.dry_run,
            "executed": self.executed,
            "skipped": self.skipped,
            "manual": self.manual,
            "failed": self.failed,
            "backup_dir": self.backup_dir,
            "operations": self.operations,
        }


def _target(config: Config, table: str, rel: str, *, must_exist: bool = False) -> Path:
    try:
        return safe_join(config.replica_root, f"{config.table(table).path}/{rel}", must_exist=must_exist)
    except LakeMirrorError as exc:
        raise ExecutionError(f"target rejected for {table}/{rel}: {exc}", "TARGET_PATH_UNSAFE") from exc


def _source(config: Config, table: str, rel: str) -> Path:
    try:
        path = safe_join(config.primary_root, f"{config.table(table).path}/{rel}", must_exist=True)
    except LakeMirrorError as exc:
        raise ExecutionError(f"source rejected for {table}/{rel}: {exc}", "SOURCE_PATH_UNSAFE") from exc
    if path.is_symlink():
        raise ExecutionError(f"source must not be a symlink: {table}/{rel}", "SYMLINK_ESCAPE")
    return path


def _matches(path: Path, expected: dict[str, Any]) -> bool:
    if not path.is_file():
        return False
    try:
        facts = file_facts(path)
    except LakeMirrorError:
        return False
    return all(key in facts and facts[key] == value for key, value in expected.items() if key in {"size", "sha256", "row_count"})


def _read_pointer(path: Path) -> str | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value.get("snapshot_id") if isinstance(value, dict) else None


def _snapshot_exists(config: Config, table: str, snapshot_id: str) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", snapshot_id) or ".." in snapshot_id:
        return False
    root = Path(config.replica_root).joinpath(*config.table(table).path.split("/"))
    try:
        snapshot = safe_join(root, f"_snapshots/{snapshot_id}.json")
    except LakeMirrorError:
        return False
    return snapshot.is_file() or _read_manifest_snapshot(root / "_manifest.json") == snapshot_id


def _read_manifest_snapshot(path: Path) -> str | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value.get("snapshot_id") if isinstance(value, dict) else None


def _preflight(config: Config, plan: dict[str, Any]) -> None:
    for operation in plan["operations"]:
        if operation["type"] == "manual_review":
            continue
        table = operation["table"]
        if table not in config.tables:
            raise ExecutionError(f"operation references unknown table: {table}")
        target = _target(config, table, operation["target_rel"])
        if operation["type"] in {"copy_file", "replace_file", "copy_manifest"}:
            source = _source(config, table, operation["source_rel"])
            if source == target:
                raise ExecutionError("source and target are identical")
            expected = operation.get("expected") or {}
            if not _matches(source, expected):
                raise ExecutionError(f"source no longer matches the plan: {source}", "SOURCE_CHANGED")
        if operation["type"] == "delete_file" and not config.repair.allow_delete:
            raise ExecutionError("delete operations are disabled by configuration", "DELETE_FORBIDDEN")
        if operation["type"] == "update_replica_pointer":
            snapshot_id = (operation.get("expected") or {}).get("snapshot_id")
            if not isinstance(snapshot_id, str) or not _snapshot_exists(config, table, snapshot_id):
                raise ExecutionError("pointer target snapshot is absent from replica history", "POINTER_TARGET_MISSING")


def _record_change(config: Config, batch_dir: Path, table: str, rel: str, target: Path,
                   operation: dict[str, Any], changes: list[dict[str, Any]]) -> None:
    record = {
        "op_id": operation["op_id"],
        "table": table,
        "target_rel": rel,
        "original_exists": target.is_file(),
        "backup_rel": None,
    }
    if target.is_file():
        if target.is_symlink():
            raise ExecutionError(f"refusing to back up a symlink: {table}/{rel}", "SYMLINK_ESCAPE")
        try:
            backup = safe_join(batch_dir, f"{config.table(table).path}/{rel}")
        except LakeMirrorError as exc:
            raise ExecutionError(f"backup path rejected: {exc}", "BACKUP_PATH_UNSAFE") from exc
        atomic_copy(target, backup)
        record["backup_rel"] = f"{config.table(table).path}/{rel}"
    changes.append(record)


def _write_pointer(config: Config, table: str, snapshot_id: str) -> None:
    target = _target(config, table, "_pointer.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, pretty_json({"table": table, "snapshot_id": snapshot_id}))


def _apply_operation(config: Config, operation: dict[str, Any], batch_dir: Path,
                     changes: list[dict[str, Any]]) -> str:
    kind = operation["type"]
    table, rel = operation["table"], operation["target_rel"]
    target = _target(config, table, rel)
    if kind in {"copy_file", "replace_file", "copy_manifest"}:
        if _matches(target, operation.get("expected") or {}):
            return "skipped_already_satisfied"
        _record_change(config, batch_dir, table, rel, target, operation, changes)
        source = _source(config, table, operation["source_rel"])
        atomic_copy(source, target)
        if not _matches(target, operation.get("expected") or {}):
            raise ExecutionError(f"post-write verification failed for {table}/{rel}", "POST_WRITE_MISMATCH")
        return "succeeded"
    if kind == "delete_file":
        if not target.exists():
            return "skipped_already_satisfied"
        _record_change(config, batch_dir, table, rel, target, operation, changes)
        target.unlink()
        if target.exists():
            raise ExecutionError(f"delete verification failed for {table}/{rel}", "POST_DELETE_MISMATCH")
        return "succeeded"
    if kind == "update_replica_pointer":
        snapshot_id = (operation.get("expected") or {})["snapshot_id"]
        if _read_pointer(target) == snapshot_id:
            return "skipped_already_satisfied"
        _record_change(config, batch_dir, table, rel, target, operation, changes)
        _write_pointer(config, table, snapshot_id)
        if _read_pointer(target) != snapshot_id:
            raise ExecutionError("pointer verification failed", "POST_WRITE_MISMATCH")
        return "succeeded"
    raise ExecutionError(f"unsupported operation type: {kind}")


def _set_status(ledger: Ledger | None, result: ExecutionResult, plan: dict[str, Any],
                operation: dict[str, Any], status: str, evidence: dict[str, Any]) -> None:
    if ledger is None:
        return
    ledger.set_operation(plan["plan_id"], result.batch_id, operation, status, evidence, utc_now())
    ledger.append("operation_" + status, {"op_id": operation["op_id"], **evidence}, utc_now(), batch_id=result.batch_id)


def execute_plan(config: Config, plan: dict[str, Any], execute: bool = False,
                 ledger: Ledger | None = None, workdir: str | Path | None = None) -> ExecutionResult:
    validate_plan(plan)
    if Path(plan["config_path"]).resolve() != config.config_path.resolve():
        raise ExecutionError("plan config path does not match the loaded configuration", "CONFIG_MISMATCH")
    if plan["config_fingerprint"] != config.fingerprint:
        raise ExecutionError("plan/config fingerprint mismatch", "CONFIG_FINGERPRINT_MISMATCH")
    if Path(plan["workdir"]).resolve() != config.workdir.resolve():
        raise ExecutionError("plan workdir does not match the loaded configuration", "WORKDIR_MISMATCH")
    if workdir is not None and Path(workdir).resolve() != config.workdir.resolve():
        raise ExecutionError("--workdir does not match the plan and configuration", "WORKDIR_MISMATCH")
    if execute and not plan["executable"]:
        raise ExecutionError("review-only plan cannot be executed", "PLAN_NOT_EXECUTABLE")
    if len(plan["operations"]) > config.repair.max_operations:
        raise ExecutionError("plan exceeds the configured operation limit", "PLAN_LIMIT_EXCEEDED")
    _preflight(config, plan)
    stamp = time.time_ns()
    result = ExecutionResult(
        batch_id=f"{'dry' if not execute else 'batch'}-{stamp}",
        status="dry_run" if not execute else "running",
        dry_run=not execute,
    )
    batch_dir = Path(config.repair.backup_dir) / result.batch_id
    if not within(config.workdir, batch_dir):
        raise ExecutionError("backup directory escapes workdir", "BACKUP_PATH_UNSAFE")
    changes: list[dict[str, Any]] = []
    if execute:
        batch_dir.mkdir(parents=True, exist_ok=True)
        result.backup_dir = str(batch_dir)
    if ledger:
        ledger.create_batch(result.batch_id, plan["plan_id"], result.status, result.backup_dir, utc_now(), {})
        ledger.append("batch_started", {"dry_run": not execute, "plan_id": plan["plan_id"]}, utc_now(), batch_id=result.batch_id)
    for operation in plan["operations"]:
        if operation["type"] == "manual_review":
            result.manual += 1
            result.operations.append({"op_id": operation["op_id"], "status": "skipped_manual", "type": "manual_review"})
            _set_status(ledger, result, plan, operation, "skipped_manual", {"reason": operation["reason"]})
            continue
        if not execute:
            if _operation_satisfied(config, operation):
                status = "skipped_already_satisfied"
                result.skipped += 1
            else:
                status = "would_execute"
                result.executed += 1
            result.operations.append({"op_id": operation["op_id"], "status": status, "type": operation["type"]})
            _set_status(ledger, result, plan, operation, status, {"dry_run": True})
            continue
        try:
            status = _apply_operation(config, operation, batch_dir, changes)
            result.skipped += int(status == "skipped_already_satisfied")
            result.executed += int(status == "succeeded")
            result.operations.append({"op_id": operation["op_id"], "status": status, "type": operation["type"]})
            _set_status(ledger, result, plan, operation, status, {"target": operation["target_rel"]})
        except (LakeMirrorError, OSError) as exc:
            result.failed += 1
            result.operations.append({"op_id": operation["op_id"], "status": "failed", "type": operation["type"], "error": str(exc)})
            _set_status(ledger, result, plan, operation, "failed", {"error": str(exc)})
            result.status = "failed"
            break
    if execute:
        atomic_write_text(batch_dir / "changes.json", pretty_json({"batch_id": result.batch_id, "changes": changes}))
    if result.status == "running":
        result.status = "completed"
    summary = result.as_dict()
    summary.pop("operations", None)
    if ledger:
        final_status = "failed" if result.status == "failed" else ("dry_run" if not execute else "completed")
        ledger.finish_batch(result.batch_id, final_status, utc_now(), summary)
        ledger.append("batch_finished", summary, utc_now(), batch_id=result.batch_id)
    return result


def _operation_satisfied(config: Config, operation: dict[str, Any]) -> bool:
    kind = operation["type"]
    target = _target(config, operation["table"], operation["target_rel"])
    if kind in {"copy_file", "replace_file", "copy_manifest"}:
        return _matches(target, operation.get("expected") or {})
    if kind == "delete_file":
        return not target.exists()
    if kind == "update_replica_pointer":
        return _read_pointer(target) == operation["expected"]["snapshot_id"]
    return False

