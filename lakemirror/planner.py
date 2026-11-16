"""Deterministic, explainable repair-plan construction."""

from __future__ import annotations

from typing import Any, Iterable

from .config import Config
from .plan_io import (
    OP_TYPES,
    PlanError,
    load_plan,
    refresh_fingerprint,
    validate_plan,
)
from .util import LakeMirrorError, canonical_json, file_facts, fingerprint, safe_join

FILE_CODES = {
    "FILE_MISSING_IN_REPLICA",
    "FILE_EXTRA_IN_REPLICA",
    "FILE_SIZE_MISMATCH",
    "FILE_SHA256_MISMATCH",
    "ROW_COUNT_MISMATCH",
}
DELETE_CODES = {"FILE_EXTRA_IN_REPLICA"}
PARTITION_CODES = {"PARTITION_MISSING", "PARTITION_EXTRA"}
NEVER_AUTO = {
    "DUPLICATE_LOGICAL_PATH",
    "PATH_OUTSIDE_ROOT",
    "INVALID_PARTITION_VALUE",
    "SNAPSHOT_LAG",
    "SNAPSHOT_CHAIN_BROKEN",
    "SNAPSHOT_FORK",
    "SCHEMA_ADDITIVE_OK",
    "SCHEMA_TYPE_MISMATCH",
    "SCHEMA_FIELD_MISSING",
    "SCHEMA_FIELD_ORDER_MISMATCH",
    "SCHEMA_NULLABILITY_MISMATCH",
    "PARTITION_SPEC_MISMATCH",
    "PARTITION_COLUMN_MISSING",
}
OP_PRIORITY = {
    "copy_file": 10,
    "replace_file": 20,
    "delete_file": 30,
    "copy_manifest": 80,
    "update_replica_pointer": 90,
    "manual_review": 0,
}


def _effective_auto(config: Config, diff: dict[str, Any]) -> bool:
    code = diff["code"]
    if not diff.get("auto_fixable") or not config.can_auto(code) or code in NEVER_AUTO:
        return False
    if code in DELETE_CODES and not config.repair.allow_delete:
        return False
    return True


def _blocked_tables(config: Config, active: list[dict[str, Any]]) -> set[str]:
    return {
        diff["table"]
        for diff in active
        if not _effective_auto(config, diff) and diff["code"] != "SCHEMA_ADDITIVE_OK"
    }


def _file_expected(config: Config, table: str, rel: str) -> dict[str, Any]:
    try:
        source = safe_join(config.primary_root, f"{config.table(table).path}/{rel}", must_exist=True)
        target = safe_join(config.replica_root, f"{config.table(table).path}/{rel}")
    except LakeMirrorError as exc:
        raise PlanError(f"{table}/{rel}: {exc}", "PLAN_PATH_UNSAFE") from exc
    if target == source:
        raise PlanError(f"{table}/{rel}: source and target are identical")
    return file_facts(source)


def _file_operation(config: Config, table: str, partition: str, rel: str, op_type: str,
                    diff_codes: Iterable[str]) -> dict[str, Any]:
    expected = _file_expected(config, table, rel)
    destructive = op_type in {"replace_file", "delete_file"}
    rollback = "restore batch backup, or remove a newly copied file"
    preconditions = [
        "plan fingerprint and config fingerprint match",
        "source and target remain inside their configured roots",
        "source matches the expected size, sha256, and row_count",
    ]
    if destructive:
        preconditions.append("a batch backup exists before mutation")
    return {
        "type": op_type,
        "table": table,
        "partition": partition,
        "path": rel,
        "source_rel": rel,
        "target_rel": rel,
        "preconditions": preconditions,
        "expected": expected,
        "impact": {"table": table, "partition": partition, "files": 1, "bytes": expected["size"], "destructive": destructive},
        "auto_executable": True,
        "rollback": rollback,
        "diff_codes": sorted(set(diff_codes)),
    }


def _delete_operation(partition: str, rel: str, diff_codes: Iterable[str], size: int) -> dict[str, Any]:
    return {
        "type": "delete_file",
        "table": "",
        "partition": partition,
        "path": rel,
        "source_rel": None,
        "target_rel": rel,
        "preconditions": ["allow_delete is true", "target remains inside replica root", "a batch backup exists"],
        "expected": {"absent": True},
        "impact": {"partition": partition, "files": 1, "bytes": size, "destructive": True},
        "auto_executable": True,
        "rollback": "restore the deleted file from the batch backup",
        "diff_codes": sorted(set(diff_codes)),
    }


def _build_file_ops(config: Config, active: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], set[tuple[str, str, str]]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for diff in active:
        if diff["code"] in FILE_CODES and _effective_auto(config, diff) and diff.get("path"):
            grouped.setdefault((diff["table"], diff["partition"], diff["path"]), []).append(diff)
    operations: list[dict[str, Any]] = []
    covered: set[tuple[str, str, str]] = set()
    for (table, partition, rel), diffs in sorted(grouped.items()):
        codes = {item["code"] for item in diffs}
        if "FILE_MISSING_IN_REPLICA" in codes:
            operation = _file_operation(config, table, partition, rel, "copy_file", codes)
        elif "FILE_EXTRA_IN_REPLICA" in codes:
            try:
                safe_join(config.replica_root, f"{config.table(table).path}/{rel}")
            except LakeMirrorError as exc:
                raise PlanError(f"{table}/{rel}: {exc}", "PLAN_PATH_UNSAFE") from exc
            operation = _delete_operation(partition, rel, codes, int(diffs[0]["actual"].get("size", 0)))
            operation["table"] = table
        else:
            operation = _file_operation(config, table, partition, rel, "replace_file", codes)
        operations.append(operation)
        covered.add((table, partition, rel))
    return operations, covered


def _manual_review(diff: dict[str, Any], reason: str | None = None) -> dict[str, Any]:
    return {
        "type": "manual_review",
        "table": diff["table"],
        "partition": diff.get("partition", ""),
        "path": diff.get("path", ""),
        "source_rel": None,
        "target_rel": None,
        "preconditions": ["human owner confirms the expected state"],
        "expected": diff.get("expected"),
        "impact": {"table": diff["table"], "partition": diff.get("partition", ""), "blocking": True},
        "auto_executable": False,
        "rollback": "none; manual operations are never executed by this tool",
        "reason": reason or diff.get("suggestion") or f"manual review required for {diff['code']}",
        "diff_codes": [diff["code"]],
    }


def _manifest_op(config: Config, table: str, diff_codes: Iterable[str]) -> dict[str, Any]:
    expected = _file_expected(config, table, "_manifest.json")
    return {
        "type": "copy_manifest",
        "table": table,
        "partition": "",
        "path": "_manifest.json",
        "source_rel": "_manifest.json",
        "target_rel": "_manifest.json",
        "preconditions": [
            "plan fingerprint and config fingerprint match",
            "all file operations for the table succeeded",
            "source manifest hash matches the planned value",
        ],
        "expected": expected,
        "impact": {"table": table, "files": 1, "bytes": expected["size"], "destructive": False},
        "auto_executable": True,
        "rollback": "restore the previous manifest from the batch backup",
        "diff_codes": sorted(set(diff_codes)),
    }


def _pointer_op(table: str, expected_id: str) -> dict[str, Any]:
    return {
        "type": "update_replica_pointer",
        "table": table,
        "partition": "",
        "path": "_pointer.json",
        "source_rel": None,
        "target_rel": "_pointer.json",
        "preconditions": [
            "plan fingerprint and config fingerprint match",
            "the target snapshot exists in replica history",
            "a batch backup of the old pointer exists",
        ],
        "expected": {"snapshot_id": expected_id},
        "impact": {"table": table, "files": 1, "destructive": False},
        "auto_executable": True,
        "rollback": "restore the previous pointer from the batch backup",
        "diff_codes": ["POINTER_STALE"],
    }


def _sort_key(operation: dict[str, Any]) -> tuple[Any, ...]:
    manual = operation["type"] == "manual_review"
    partition = operation["partition"] if operation["type"] in {"copy_file", "replace_file", "delete_file"} else "￿"
    return (
        0 if manual else 1,
        operation["table"],
        partition,
        OP_PRIORITY[operation["type"]],
        operation["path"],
        canonical_json(operation.get("diff_codes") or []),
    )


def build_plan(config: Config, scan_id: str, diffs: list[dict[str, Any]], kind: str = "executable") -> dict[str, Any]:
    if kind not in {"executable", "dry-run"}:
        raise PlanError("kind must be executable or dry-run")
    active = [dict(item) for item in diffs if not item.get("ignored")]
    blocked = _blocked_tables(config, active)
    safe_active = [item for item in active if item["table"] not in blocked]
    file_ops, covered = _build_file_ops(config, safe_active)
    operations: list[dict[str, Any]] = list(file_ops)
    auto_file_keys = {(item["table"], item["partition"], item["path"]) for item in file_ops}
    for diff in active:
        code = diff["code"]
        if code == "SCHEMA_ADDITIVE_OK" and diff["table"] not in blocked:
            continue
        if diff["table"] not in blocked:
            if code in FILE_CODES and _effective_auto(config, diff) and diff.get("path"):
                continue
            if code in PARTITION_CODES and _effective_auto(config, diff):
                same = [item for item in file_ops if item["table"] == diff["table"] and item["partition"] == diff["partition"]]
                if same:
                    continue
            if code == "POINTER_STALE" and _effective_auto(config, diff):
                snapshot_id = (diff.get("expected") or {}).get("snapshot_id")
                if isinstance(snapshot_id, str) and snapshot_id:
                    operations.append(_pointer_op(diff["table"], snapshot_id))
                    continue
            if diff.get("auto_fixable") and code in FILE_CODES and (diff["table"], diff["partition"], diff["path"]) in auto_file_keys:
                continue
        operations.append(_manual_review(diff))
    for table in config.tables:
        table_diffs = [item for item in active if item["table"] == table]
        has_file_ops = any(item["table"] == table for item in file_ops)
        if has_file_ops:
            operations.append(_manifest_op(config, table, [item["code"] for item in table_diffs if item["code"] in FILE_CODES]))
    operations.sort(key=_sort_key)
    for index, operation in enumerate(operations, 1):
        digest = fingerprint({key: value for key, value in operation.items() if key != "op_id"})[:8]
        operation["op_id"] = f"op-{index:04d}-{digest}"
    if len(operations) > config.repair.max_operations:
        raise PlanError(f"plan has {len(operations)} operations, above limit {config.repair.max_operations}", "PLAN_LIMIT_EXCEEDED")
    manual_count = sum(1 for item in operations if item["type"] == "manual_review")
    core = {
        "schema_version": 1,
        "kind": kind,
        "executable": kind == "executable",
        "scan_id": scan_id,
        "config_fingerprint": config.fingerprint,
        "config_path": str(config.config_path),
        "workdir": str(config.workdir),
        "operation_count": len(operations),
        "manual_review_count": manual_count,
        "operations": operations,
    }
    return refresh_fingerprint(core)
