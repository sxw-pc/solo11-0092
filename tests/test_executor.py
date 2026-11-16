"""Sandboxed execution, idempotency, verification, and rollback tests."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from lakemirror.executor import ExecutionError, execute_plan
from lakemirror.ledger import Ledger
from lakemirror.plan_io import PlanError, refresh_fingerprint
from lakemirror.reconcile import scan_config
from lakemirror.rollback import RollbackError, rollback_batch
from lakemirror.util import sha256_file, utc_now
from lakemirror.verify import verify_batch
from tests.support import make_config


def tree_hash(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)).replace("\\", "/"): sha256_file(path)
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def build(config, kind: str = "executable") -> dict:
    from lakemirror.planner import build_plan
    result = scan_config(config, "scan-exec")
    return build_plan(config, result.scan_id, [item.as_dict() for item in result.diffs], kind)


def store_scan_and_plan(ledger: Ledger, config, plan: dict) -> None:
    result = scan_config(config, plan["scan_id"])
    now = utc_now()
    ledger.save_scan(result, now, now)
    ledger.save_plan(plan, now)


class ExecutorTests(unittest.TestCase):
    def test_dry_run_does_not_touch_replica(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            plan = build(config)
            before = tree_hash(Path(config.replica_root))
            with Ledger(Path(tmp) / "ledger.db") as ledger:
                result = execute_plan(config, plan, execute=False, ledger=ledger, workdir=config.workdir)
                batch = ledger.get_batch(result.batch_id)
            self.assertEqual(result.status, "dry_run")
            self.assertTrue(result.dry_run)
            self.assertEqual(result.executed, 11)
            self.assertEqual(result.skipped, 0)
            self.assertIsNone(result.backup_dir)
            self.assertEqual(tree_hash(Path(config.replica_root)), before)
            self.assertEqual(batch["status"], "dry_run")
            self.assertFalse((Path(config.workdir) / "backups").exists())

    def test_execute_repairs_replica_and_writes_backups(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            plan = build(config)
            with Ledger(Path(tmp) / "ledger.db") as ledger:
                store_scan_and_plan(ledger, config, plan)
                result = execute_plan(config, plan, execute=True, ledger=ledger, workdir=config.workdir)
                batch = ledger.get_batch(result.batch_id)
                operation_rows = ledger.get_operations(plan["plan_id"])
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.executed, 11)
            self.assertEqual(result.failed, 0)
            backup = Path(result.backup_dir)
            self.assertTrue(backup.is_dir())
            changes = json.loads((backup / "changes.json").read_text(encoding="utf-8"))["changes"]
            self.assertEqual(len(changes), 11)
            self.assertTrue(any(item["original_exists"] for item in changes))
            self.assertTrue(any(not item["original_exists"] for item in changes))
            self.assertEqual(batch["status"], "completed")
            self.assertEqual(sum(1 for item in operation_rows if item["status"] == "succeeded"), 11)
            after = scan_config(config, "scan-after")
            self.assertEqual([item for item in after.diffs if not item.ignored], [])

    def test_verify_succeeds_after_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            plan = build(config)
            with Ledger(Path(tmp) / "ledger.db") as ledger:
                store_scan_and_plan(ledger, config, plan)
                result = execute_plan(config, plan, execute=True, ledger=ledger)
                batch = ledger.get_batch(result.batch_id)
                report = verify_batch(config, batch, ledger, config.workdir)
            self.assertEqual(report["status"], "verified")
            self.assertEqual(report["auto_residual"], [])
            self.assertEqual(report["failed_operations"], [])
            self.assertTrue(report["scan_id"])

    def test_rerun_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            plan = build(config)
            first = execute_plan(config, plan, execute=True)
            self.assertEqual(first.executed, 11)
            second = execute_plan(config, plan, execute=True)
            self.assertEqual(second.status, "completed")
            self.assertEqual(second.executed, 0)
            self.assertEqual(second.skipped, 11)
            with Ledger(Path(tmp) / "ledger.db") as ledger:
                dry = execute_plan(config, plan, execute=False, ledger=ledger)
            self.assertEqual(dry.skipped, 11)

    def test_dry_run_plan_cannot_be_executed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            plan = build(config, "dry-run")
            with self.assertRaises(ExecutionError) as caught:
                execute_plan(config, plan, execute=True)
            self.assertEqual(caught.exception.code, "PLAN_NOT_EXECUTABLE")

    def test_config_fingerprint_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            plan = copy.deepcopy(build(config))
            plan["config_fingerprint"] = "0" * 64
            plan = refresh_fingerprint(plan)
            with self.assertRaises(ExecutionError) as caught:
                execute_plan(config, plan, execute=True)
            self.assertEqual(caught.exception.code, "CONFIG_FINGERPRINT_MISMATCH")

    def test_changed_source_is_rejected_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            plan = build(config)
            copy_op = next(item for item in plan["operations"] if item["type"] == "copy_file")
            source = Path(config.primary_root).joinpath(
                *config.table(copy_op["table"]).path.split("/"), *copy_op["source_rel"].split("/"))
            source.write_bytes(source.read_bytes() + b"tampered\n")
            before = tree_hash(Path(config.replica_root))
            with self.assertRaises(ExecutionError) as caught:
                execute_plan(config, plan, execute=True)
            self.assertEqual(caught.exception.code, "SOURCE_CHANGED")
            self.assertEqual(tree_hash(Path(config.replica_root)), before)

    def test_tampered_plan_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            plan = copy.deepcopy(build(config))
            plan["operations"][0]["target_rel"] = "../../outside.csv"
            plan = refresh_fingerprint(plan)
            with self.assertRaises(PlanError) as caught:
                execute_plan(config, plan, execute=True)
            self.assertEqual(caught.exception.code, "PLAN_PATH_UNSAFE")

    def test_manual_plan_executes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, scenario="schema_drift")
            plan = build(config)
            before = tree_hash(Path(config.replica_root))
            result = execute_plan(config, plan, execute=True)
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.manual, 14)
            self.assertEqual(result.executed, 0)
            self.assertEqual(result.skipped, 0)
            self.assertEqual(tree_hash(Path(config.replica_root)), before)
            changes = json.loads((Path(result.backup_dir) / "changes.json").read_text(encoding="utf-8"))
            self.assertEqual(changes["changes"], [])


class RollbackTests(unittest.TestCase):
    def _executed(self, tmp: str):
        config = make_config(tmp)
        plan = build(config)
        ledger = Ledger(Path(tmp) / "ledger.db")
        store_scan_and_plan(ledger, config, plan)
        result = execute_plan(config, plan, execute=True, ledger=ledger)
        batch = ledger.get_batch(result.batch_id)
        return config, plan, result, ledger, batch

    def test_rollback_dry_run_then_execute_restores_replica(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            original = tree_hash(Path(config.replica_root))
            plan = build(config)
            with Ledger(Path(tmp) / "ledger.db") as ledger:
                store_scan_and_plan(ledger, config, plan)
                result = execute_plan(config, plan, execute=True, ledger=ledger)
                batch = ledger.get_batch(result.batch_id)
                preview = rollback_batch(config, batch, execute=False, ledger=ledger)
                self.assertEqual(preview["status"], "dry_run")
                self.assertEqual(len(preview["actions"]), 11)
                self.assertNotEqual(tree_hash(Path(config.replica_root)), original)
                record = rollback_batch(config, batch, execute=True, ledger=ledger)
                self.assertEqual(record["status"], "rolled_back")
                rolled = ledger.get_batch(result.batch_id)
            self.assertEqual(rolled["status"], "rolled_back")
            self.assertEqual(tree_hash(Path(config.replica_root)), original)
            self.assertTrue((Path(result.backup_dir) / "rollback.json").is_file())
            after = scan_config(config, "scan-rolled-back")
            self.assertEqual(len([item for item in after.diffs if not item.ignored]), 14)

    def test_rollback_refuses_dry_run_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            plan = build(config)
            with Ledger(Path(tmp) / "ledger.db") as ledger:
                result = execute_plan(config, plan, execute=False, ledger=ledger)
                batch = ledger.get_batch(result.batch_id)
            with self.assertRaises(RollbackError) as caught:
                rollback_batch(config, batch, execute=True)
            self.assertEqual(caught.exception.code, "ROLLBACK_DRY_RUN")

    def test_rollback_refuses_second_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config, _plan, _result, ledger, batch = self._executed(tmp)
            try:
                rollback_batch(config, batch, execute=True, ledger=ledger)
                with self.assertRaises(RollbackError) as caught:
                    rollback_batch(config, batch, execute=True, ledger=ledger)
                self.assertEqual(caught.exception.code, "ROLLBACK_ALREADY_DONE")
            finally:
                ledger.close()


if __name__ == "__main__":
    unittest.main()
