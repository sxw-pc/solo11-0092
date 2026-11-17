"""Safe batch rollback for replica-sandbox changes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import Config
from .util import (
    LakeMirrorError,
    atomic_copy,
    atomic_write_text,
    pretty_json,
    safe_join,
    utc_now,
    within,
)


class RollbackError(LakeMirrorError):
    def __init__(self, message: str, code: str = "ROLLBACK_REFUSED") -> None:
        super().__init__(message, code)


def _preflight(config: Config, batch: dict[str, Any]) -> tuple[Path, list[dict[str, Any]]]:
    if batch.get("status") == "dry_run":
        raise RollbackError("a dry-run batch has no files to roll back", "ROLLBACK_DRY_RUN")
    if batch.get("status") == "rolled_back":
        raise RollbackError("batch is already rolled back", "ROLLBACK_ALREADY_DONE")
    backup_raw = batch.get("backup_dir")
    if not backup_raw:
        raise RollbackError("batch has no backup directory", "ROLLBACK_PREREQUISITE")
    backup_dir = Path(backup_raw).resolve(strict=False)
    if backup_dir.is_symlink() or not within(config.workdir, backup_dir):
        raise RollbackError("backup directory is outside workdir or is a symlink", "ROLLBACK_PATH_UNSAFE")
    manifest = backup_dir / "changes.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RollbackError(f"backup manifest is missing: {manifest}", "ROLLBACK_PREREQUISITE") from exc
    except json.JSONDecodeError as exc:
        raise RollbackError(f"backup manifest is invalid: {exc}", "ROLLBACK_PREREQUISITE") from exc
    changes = data.get("changes") if isinstance(data, dict) else None
    if not isinstance(changes, list):
        raise RollbackError("backup manifest has no changes array", "ROLLBACK_PREREQUISITE")
    checked = []
    for change in changes:
        if not isinstance(change, dict):
            raise RollbackError("backup change entry is invalid", "ROLLBACK_PREREQUISITE")
        table, rel = change.get("table"), change.get("target_rel")
        if table not in config.tables or not isinstance(rel, str):
            raise RollbackError("backup entry references an unknown table/path", "ROLLBACK_PREREQUISITE")
        try:
            safe_join(config.replica_root, f"{config.table(table).path}/{rel}")
        except LakeMirrorError as exc:
            raise RollbackError(f"rollback target rejected: {exc}", "ROLLBACK_PATH_UNSAFE") from exc
        if change.get("original_exists"):
            backup_rel = change.get("backup_rel")
            if not isinstance(backup_rel, str):
                raise RollbackError("backup entry lacks backup_rel", "ROLLBACK_PREREQUISITE")
            try:
                backup = safe_join(backup_dir, backup_rel, must_exist=True)
            except LakeMirrorError as exc:
                raise RollbackError(f"backup file unavailable: {exc}", "ROLLBACK_PREREQUISITE") from exc
            if not backup.is_file():
                raise RollbackError(f"backup file unavailable: {backup_rel}", "ROLLBACK_PREREQUISITE")
        checked.append(change)
    return backup_dir, checked


def rollback_batch(config: Config, batch: dict[str, Any], execute: bool = False,
                   ledger: Any = None, workdir: str | Path | None = None) -> dict[str, Any]:
    if workdir is not None and Path(workdir).resolve() != config.workdir.resolve():
        raise RollbackError("--workdir does not match the configuration", "WORKDIR_MISMATCH")
    if ledger is not None:
        current = ledger.get_batch(batch["batch_id"])
        if current is not None:
            batch = current
    backup_dir, changes = _preflight(config, batch)
    actions = []
    for change in reversed(changes):
        actions.append(
            {
                "table": change["table"],
                "target_rel": change["target_rel"],
                "restore": "backup" if change.get("original_exists") else "remove_new_file",
            }
        )
    if not execute:
        return {"batch_id": batch["batch_id"], "status": "dry_run", "actions": actions}
    try:
        for change in reversed(changes):
            table, rel = change["table"], change["target_rel"]
            target = safe_join(config.replica_root, f"{config.table(table).path}/{rel}")
            if change.get("original_exists"):
                backup = safe_join(backup_dir, change["backup_rel"], must_exist=True)
                atomic_copy(backup, target)
            elif target.exists():
                target.unlink()
        record = {"batch_id": batch["batch_id"], "status": "rolled_back", "actions": actions, "finished_at": utc_now()}
        atomic_write_text(backup_dir / "rollback.json", pretty_json(record))
        if ledger is not None:
            ledger.finish_batch(batch["batch_id"], "rolled_back", utc_now(), record)
            ledger.append("batch_rolled_back", record, utc_now(), batch_id=batch["batch_id"])
        return record
    except (LakeMirrorError, OSError) as exc:
        record = {"batch_id": batch.get("batch_id"), "status": "rollback_failed", "error": str(exc)}
        if ledger is not None:
            ledger.finish_batch(batch["batch_id"], "rollback_failed", utc_now(), record)
            ledger.append("batch_rollback_failed", record, utc_now(), batch_id=batch.get("batch_id"))
        raise RollbackError(f"rollback failed and stopped: {exc}", "ROLLBACK_FAILED") from exc
