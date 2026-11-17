"""Post-execution verification against a fresh reconciliation scan."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import Config
from .ledger import Ledger
from .plan_io import PlanError, load_plan
from .reconcile import scan_config
from .util import LakeMirrorError, utc_now


class VerifyError(LakeMirrorError):
    pass


def verify_batch(config: Config, batch: dict[str, Any], ledger: Ledger,
                 workdir: str | Path | None = None) -> dict[str, Any]:
    if workdir is not None and Path(workdir).resolve() != config.workdir.resolve():
        raise VerifyError("--workdir does not match the configuration", "WORKDIR_MISMATCH")
    if batch.get("status") != "completed":
        return {
            "status": "verify_failed",
            "batch_id": batch.get("batch_id"),
            "reason": f"batch status is {batch.get('status')!r}, not completed",
            "auto_residual": [],
            "manual_remaining": [],
            "failed_operations": [],
        }
    try:
        plan = load_plan_for_verify(ledger, batch["plan_id"])
    except PlanError as exc:
        raise VerifyError(f"cannot load plan for verification: {exc}", "VERIFY_PLAN_MISSING") from exc
    started = utc_now()
    result = scan_config(config)
    ledger.save_scan(result, started, utc_now(), status="verified")
    failed = [item for item in ledger.get_operations(plan["plan_id"]) if item["status"] == "failed"]
    auto_residual = [item.as_dict() for item in result.diffs if item.auto_fixable and not item.ignored]
    manual = [item.as_dict() for item in result.diffs if not item.auto_fixable and not item.ignored]
    planned_manual = [item for item in plan["operations"] if item["type"] == "manual_review"]
    status = "verified" if not auto_residual and not failed else "verify_failed"
    report = {
        "status": status,
        "batch_id": batch["batch_id"],
        "scan_id": result.scan_id,
        "auto_residual": auto_residual,
        "manual_remaining": manual,
        "planned_manual_count": len(planned_manual),
        "manual_preserved_count": len(manual),
        "failed_operations": failed,
        "checked_at": utc_now(),
    }
    ledger.append(
        "verify_" + status,
        {"scan_id": result.scan_id, "auto_residual": len(auto_residual), "manual": len(manual)},
        utc_now(),
        batch_id=batch["batch_id"],
    )
    return report


def load_plan_for_verify(ledger: Ledger, plan_id: str) -> dict[str, Any]:
    plan = ledger.get_plan(plan_id)
    if plan is None:
        raise PlanError(f"plan not found in ledger: {plan_id}", "PLAN_NOT_FOUND")
    from .plan_io import validate_plan

    return validate_plan(plan)
