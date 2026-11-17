"""Deterministic repair-plan construction and plan-validation tests."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from lakemirror.plan_io import PlanError, refresh_fingerprint, validate_plan
from lakemirror.planner import build_plan
from tests.support import make_config


def plan_for(config, kind: str = "executable") -> dict:
    from lakemirror.reconcile import scan_config
    result = scan_config(config, "scan-plan")
    return build_plan(config, result.scan_id, [item.as_dict() for item in result.diffs], kind)


class PlannerTests(unittest.TestCase):
    def test_drift_plan_has_expected_operations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            plan = plan_for(config)
            types = [item["type"] for item in plan["operations"]]
            self.assertEqual(types.count("copy_file"), 3)
            self.assertEqual(types.count("replace_file"), 2)
            self.assertEqual(types.count("delete_file"), 2)
            self.assertEqual(types.count("copy_manifest"), 2)
            self.assertEqual(types.count("update_replica_pointer"), 2)
            self.assertEqual(types.count("manual_review"), 0)
            self.assertEqual(plan["operation_count"], 11)
            self.assertTrue(plan["executable"])
            self.assertEqual(plan["kind"], "executable")

    def test_operations_are_deterministically_sorted_and_identified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            first = plan_for(config)
            second = plan_for(config)
            self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
            ids = [item["op_id"] for item in first["operations"]]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertTrue(all(item.startswith(f"op-{index:04d}-") for index, item in enumerate(ids, 1)))
            self.assertTrue(first["plan_id"].startswith("plan-"))
            validate_plan(first)

    def test_file_operations_stay_inside_replica_and_carry_preconditions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            plan = plan_for(config)
            for operation in plan["operations"]:
                if operation["type"] in {"copy_file", "replace_file", "delete_file", "copy_manifest"}:
                    target = Path(config.replica_root).joinpath(
                        *config.table(operation["table"]).path.split("/"), *operation["target_rel"].split("/"))
                    self.assertTrue(target.resolve().is_relative_to(Path(config.replica_root).resolve()))
                if operation["type"] in {"copy_file", "replace_file"}:
                    self.assertIsInstance(operation["source_rel"], str)
                    self.assertEqual(operation["source_rel"], operation["path"])
                    self.assertIn("sha256", operation["expected"])
                if operation["type"] == "delete_file":
                    self.assertIsNone(operation["source_rel"])
                    self.assertTrue(operation["impact"]["destructive"])
                if operation["type"] != "manual_review":
                    self.assertGreaterEqual(len(operation["preconditions"]), 3)
                    self.assertTrue(operation["rollback"])

    def test_metadata_operations_follow_file_operations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = plan_for(make_config(tmp))
            file_indexes = [index for index, item in enumerate(plan["operations"])
                            if item["table"] == "orders"
                            and item["type"] in {"copy_file", "replace_file", "delete_file"}]
            manifest_index = next(index for index, item in enumerate(plan["operations"])
                                  if item["table"] == "orders" and item["type"] == "copy_manifest")
            pointer_index = next(index for index, item in enumerate(plan["operations"])
                                 if item["table"] == "orders" and item["type"] == "update_replica_pointer")
            self.assertLess(max(file_indexes), manifest_index)
            self.assertLess(manifest_index, pointer_index)

    def test_additive_schema_is_skipped_without_blocking_file_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            manifest_path = Path(config.replica_root) / "tables" / "orders" / "_manifest.json"
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
            document["schema"].append({"name": "note", "type": "STRING", "nullable": True})
            manifest_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            plan = plan_for(config)
            self.assertEqual(plan["operation_count"], 11)
            self.assertEqual(plan["manual_review_count"], 0)
            self.assertFalse(any("SCHEMA_ADDITIVE_OK" in item["diff_codes"] for item in plan["operations"]))

    def test_dry_run_kind_is_not_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            executable = plan_for(config, "executable")
            dry = plan_for(config, "dry-run")
            self.assertEqual(dry["kind"], "dry-run")
            self.assertFalse(dry["executable"])
            self.assertNotEqual(dry["plan_id"], executable["plan_id"])
            self.assertEqual([item["type"] for item in dry["operations"]],
                             [item["type"] for item in executable["operations"]])
            validate_plan(dry)

    def test_strict_mode_suppresses_all_table_automation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = plan_for(make_config(tmp, mode="strict"))
            self.assertTrue(all(item["type"] == "manual_review" for item in plan["operations"]))
            self.assertEqual(plan["manual_review_count"], 14)
            self.assertTrue(all(not item["auto_executable"] for item in plan["operations"]))
            self.assertTrue(all(item["reason"] for item in plan["operations"]))

    def test_allow_delete_false_never_plans_deletes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = plan_for(make_config(tmp, allow_delete=False))
            self.assertNotIn("delete_file", {item["type"] for item in plan["operations"]})
            self.assertEqual(plan["operation_count"], 14)
            self.assertEqual(plan["manual_review_count"], 14)

    def test_schema_drift_plan_is_manual_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = plan_for(make_config(tmp, scenario="schema_drift"))
            self.assertTrue(all(item["type"] == "manual_review" for item in plan["operations"]))
            self.assertEqual(plan["manual_review_count"], plan["operation_count"])
            automated = {"copy_file", "replace_file", "delete_file", "copy_manifest", "update_replica_pointer"}
            self.assertFalse(any(item["type"] in automated for item in plan["operations"]))

    def test_pointer_only_repair_is_planned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, scenario="clean_pair")
            table = config.table("orders")
            pointer = Path(config.replica_root).joinpath(*table.path.split("/")) / "_pointer.json"
            pointer.write_text('{"table": "orders", "snapshot_id": "snap-orders-0001"}\n', encoding="utf-8")
            plan = plan_for(config)
            self.assertEqual([item["type"] for item in plan["operations"]], ["update_replica_pointer"])
            operation = plan["operations"][0]
            self.assertEqual(operation["expected"]["snapshot_id"], "snap-orders-0002")
            self.assertEqual(operation["diff_codes"], ["POINTER_STALE"])
            self.assertTrue(operation["auto_executable"])

    def test_ignored_conflicts_are_not_executed(self) -> None:
        rule = {"code": "FILE_EXTRA_IN_REPLICA", "table": "events",
                "path_prefix": "data/day=2024-02-09", "reason": "held for review"}
        with tempfile.TemporaryDirectory() as tmp:
            plan = plan_for(make_config(tmp, ignore_rules=[rule]))
            deletes = [item for item in plan["operations"] if item["type"] == "delete_file"]
            self.assertEqual(len(deletes), 1)
            self.assertEqual(deletes[0]["table"], "orders")
            manual = [item for item in plan["operations"] if item["type"] == "manual_review"]
            self.assertTrue(any("PARTITION_EXTRA" in item["diff_codes"] for item in manual))

    def test_operation_limit_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, max_operations=5)
            with self.assertRaises(PlanError) as caught:
                plan_for(config)
            self.assertEqual(caught.exception.code, "PLAN_LIMIT_EXCEEDED")

    def test_outside_path_is_rejected_at_plan_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, scenario="clean_pair")
            diff = {"code": "FILE_MISSING_IN_REPLICA", "table": "orders", "partition": "dt=2024-01-01",
                    "path": "../outside.csv", "expected": {}, "actual": None,
                    "auto_fixable": True, "ignored": False}
            with self.assertRaises(PlanError) as caught:
                build_plan(config, "scan-evil", [diff])
            self.assertEqual(caught.exception.code, "PLAN_PATH_UNSAFE")

    def test_plan_validation_rejects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = plan_for(make_config(tmp))
            bad = copy.deepcopy(plan)
            bad["operations"][0]["path"] = "../escape.csv"
            with self.assertRaises(PlanError) as caught:
                validate_plan(bad)
            self.assertEqual(caught.exception.code, "PLAN_FINGERPRINT_MISMATCH")

            unsafe = copy.deepcopy(plan)
            unsafe["operations"][0]["target_rel"] = "../escape.csv"
            unsafe = refresh_fingerprint(unsafe)
            with self.assertRaises(PlanError) as caught:
                validate_plan(unsafe)
            self.assertEqual(caught.exception.code, "PLAN_PATH_UNSAFE")

            unknown = copy.deepcopy(plan)
            unknown["unexpected"] = True
            with self.assertRaises(PlanError):
                validate_plan(unknown)

            missing = copy.deepcopy(plan)
            del missing["scan_id"]
            with self.assertRaises(PlanError):
                validate_plan(missing)


if __name__ == "__main__":
    unittest.main()
