"""Plan serialization, fingerprint verification, and structural validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .util import LakeMirrorError, fingerprint, normalize_relpath

OP_TYPES = {
    "copy_file",
    "replace_file",
    "delete_file",
    "copy_manifest",
    "update_replica_pointer",
    "manual_review",
}
_TOP = {
    "schema_version",
    "plan_id",
    "plan_fingerprint",
    "kind",
    "executable",
    "scan_id",
    "config_fingerprint",
    "config_path",
    "workdir",
    "operation_count",
    "manual_review_count",
    "operations",
}


class PlanError(LakeMirrorError):
    def __init__(self, message: str, code: str = "PLAN_INVALID") -> None:
        super().__init__(message, code)


def _core(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": plan.get("schema_version"),
        "kind": plan.get("kind"),
        "executable": plan.get("executable"),
        "scan_id": plan.get("scan_id"),
        "config_fingerprint": plan.get("config_fingerprint"),
        "config_path": plan.get("config_path"),
        "workdir": plan.get("workdir"),
        "operation_count": plan.get("operation_count"),
        "manual_review_count": plan.get("manual_review_count"),
        "operations": plan.get("operations"),
    }


def refresh_fingerprint(plan: dict[str, Any]) -> dict[str, Any]:
    value = {key: plan[key] for key in _TOP if key in plan and key not in {"plan_id", "plan_fingerprint"}}
    value["plan_fingerprint"] = fingerprint(_core(value))
    value["plan_id"] = f"plan-{value['plan_fingerprint'][:16]}"
    return value


def validate_plan(plan: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise PlanError("plan must be an object")
    missing = sorted(_TOP - set(plan))
    if missing:
        raise PlanError(f"missing plan field(s): {', '.join(missing)}")
    unknown = sorted(set(plan) - _TOP)
    if unknown:
        raise PlanError(f"unknown plan field(s): {', '.join(unknown)}")
    if plan["schema_version"] != 1:
        raise PlanError("unsupported plan schema_version")
    if plan["kind"] not in {"executable", "dry-run"}:
        raise PlanError("kind must be executable or dry-run")
    if not isinstance(plan["executable"], bool):
        raise PlanError("executable must be boolean")
    if plan["kind"] == "dry-run" and plan["executable"]:
        raise PlanError("dry-run plan cannot be executable")
    if not isinstance(plan["operations"], list):
        raise PlanError("operations must be an array")
    if plan["plan_fingerprint"] != fingerprint(_core(plan)):
        raise PlanError("plan fingerprint mismatch; regenerate the plan", "PLAN_FINGERPRINT_MISMATCH")
    if plan["plan_id"] != f"plan-{plan['plan_fingerprint'][:16]}":
        raise PlanError("plan id does not match fingerprint", "PLAN_FINGERPRINT_MISMATCH")
    operations = plan["operations"]
    if plan["operation_count"] != len(operations):
        raise PlanError("operation_count does not match operations")
    manual_count = sum(1 for item in operations if item.get("type") == "manual_review")
    if plan["manual_review_count"] != manual_count:
        raise PlanError("manual_review_count does not match operations")
    seen: set[str] = set()
    for operation in operations:
        if not isinstance(operation, dict) or operation.get("type") not in OP_TYPES:
            raise PlanError("operation has an unsupported type")
        op_id = operation.get("op_id")
        if not isinstance(op_id, str) or not op_id or op_id in seen:
            raise PlanError("operation ids must be unique non-empty strings")
        seen.add(op_id)
        if operation["type"] != "manual_review":
            target = operation.get("target_rel")
            if not isinstance(target, str):
                raise PlanError(f"{op_id}: target_rel is required")
            try:
                normalize_relpath(target)
            except LakeMirrorError as exc:
                raise PlanError(f"{op_id}: unsafe target: {exc}", "PLAN_PATH_UNSAFE") from exc
            source = operation.get("source_rel")
            if operation["type"] in {"copy_file", "replace_file", "copy_manifest"}:
                if not isinstance(source, str):
                    raise PlanError(f"{op_id}: source_rel is required")
                try:
                    normalize_relpath(source)
                except LakeMirrorError as exc:
                    raise PlanError(f"{op_id}: unsafe source: {exc}", "PLAN_PATH_UNSAFE") from exc
        if operation["type"] == "manual_review" and not operation.get("reason"):
            raise PlanError(f"{op_id}: manual review requires a reason")
    return plan


def load_plan(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PlanError(f"plan not found: {path}", "PLAN_NOT_FOUND") from exc
    except json.JSONDecodeError as exc:
        raise PlanError(f"invalid plan JSON: {exc}", "PLAN_INVALID_JSON") from exc
    return validate_plan(value)
