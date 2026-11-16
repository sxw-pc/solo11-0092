"""SQLite audit-ledger persistence tests."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from lakemirror.ledger import Ledger
from lakemirror.planner import build_plan
from lakemirror.reconcile import scan_config
from lakemirror.util import utc_now
from tests.support import make_config


REQUIRED_TABLES = {"runs", "snapshots", "diffs", "plans", "operations", "batches", "ledger", "settings_snapshot"}


class LedgerTests(unittest.TestCase):
    def _scan(self, tmp: str, scenario: str = "drift_basic", ignore_rules=None):
        config = make_config(tmp, scenario=scenario, ignore_rules=ignore_rules)
        result = scan_config(config, "scan-ledger")
        return config, result

    def test_schema_contains_all_required_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.db"
            with Ledger(path):
                pass
            conn = sqlite3.connect(path)
            try:
                names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            finally:
                conn.close()
            self.assertTrue(REQUIRED_TABLES.issubset(names))

    def test_scan_round_trip_and_settings_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _config, result = self._scan(tmp)
            now = utc_now()
            path = Path(tmp) / "ledger.db"
            with Ledger(path) as ledger:
                ledger.save_scan(result, now, now)
                run = ledger.get_run("latest")
                self.assertEqual(run["run_id"], "scan-ledger")
                self.assertEqual(run["status"], "completed")
                self.assertEqual(run["stats"]["total_diffs"], 14)
                self.assertEqual(run["stats"]["table_stats"]["orders"]["primary_files"], 4)
                self.assertEqual(len(run["stats"]["snapshot_chains"]), 4)
                settings = ledger.get_settings("scan-ledger")
                self.assertEqual(settings["repair"]["mode"], "lenient")
                self.assertEqual(len(ledger.list_runs()), 1)
            with Ledger(path) as ledger:
                self.assertEqual(ledger.get_run("scan-ledger")["config_fingerprint"], result.config.fingerprint)

    def test_diffs_are_normalized_and_ignore_flags_survive(self) -> None:
        rule = {"code": "FILE_EXTRA_IN_REPLICA", "table": "events",
                "path_prefix": "data/day=2024-02-09", "reason": "audited exclusion"}
        with tempfile.TemporaryDirectory() as tmp:
            _config, result = self._scan(tmp, ignore_rules=[rule])
            now = utc_now()
            with Ledger(Path(tmp) / "ledger.db") as ledger:
                ledger.save_scan(result, now, now)
                diffs = ledger.get_diffs("scan-ledger")
            self.assertEqual(len(diffs), 14)
            self.assertTrue(all("table" in item and "table_name" not in item for item in diffs))
            ignored = [item for item in diffs if item["ignored"]]
            self.assertEqual(len(ignored), 1)
            self.assertEqual(ignored[0]["ignore_reason"], "audited exclusion")
            self.assertEqual(ignored[0]["partition"], "day=2024-02-09")
            self.assertIsInstance(ignored[0]["evidence"], dict)
            self.assertFalse(any(item["auto_fixable"] for item in ignored))

    def test_repeated_scans_keep_each_runs_diff_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            first = scan_config(config, "scan-first")
            second = scan_config(config, "scan-second")
            self.assertEqual(
                [item.diff_id for item in first.diffs],
                [item.diff_id for item in second.diffs],
            )
            now = utc_now()
            with Ledger(Path(tmp) / "ledger.db") as ledger:
                ledger.save_scan(first, now, now)
                ledger.save_scan(second, now, now)
                self.assertEqual(len(ledger.get_diffs("scan-first")), 14)
                self.assertEqual(len(ledger.get_diffs("scan-second")), 14)

    def test_snapshots_are_persisted_for_both_sides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _config, result = self._scan(tmp)
            now = utc_now()
            with Ledger(Path(tmp) / "ledger.db") as ledger:
                ledger.save_scan(result, now, now)
                snapshots = ledger.get_snapshots("scan-ledger")
            self.assertEqual(len(snapshots), 8)
            self.assertEqual({item["side"] for item in snapshots}, {"primary", "replica"})
            self.assertEqual({item["table_name"] for item in snapshots}, {"orders", "events"})
            self.assertTrue(all(isinstance(item["issues"], list) for item in snapshots))

    def test_plan_and_operations_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config, result = self._scan(tmp)
            plan = build_plan(config, result.scan_id, [item.as_dict() for item in result.diffs])
            now = utc_now()
            with Ledger(Path(tmp) / "ledger.db") as ledger:
                ledger.save_plan(plan, now)
                self.assertEqual(ledger.get_plan(plan["plan_id"])["plan_id"], plan["plan_id"])
                self.assertEqual(ledger.get_plan_by_scan("scan-ledger")["plan_id"], plan["plan_id"])
                self.assertEqual(ledger.list_plans()[0]["operation_count"], 11)
                operations = ledger.get_operations(plan["plan_id"])
                self.assertEqual(len(operations), 11)
                self.assertTrue(all(item["status"] == "pending" for item in operations))
                first = plan["operations"][0]
                ledger.set_operation(plan["plan_id"], "batch-1", first, "succeeded", {"target": first["path"]}, now)
                ledger.set_operation(plan["plan_id"], "batch-1", first, "failed", {"reason": "probe"}, now)
                changed = next(item for item in ledger.get_operations(plan["plan_id"])
                               if item["operation_id"] == first["op_id"])
                self.assertEqual(changed["status"], "failed")
                self.assertEqual(changed["evidence"], {"reason": "probe"})
                self.assertEqual(changed["batch_id"], "batch-1")

    def test_batches_exclude_dry_run_from_latest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            now = utc_now()
            with Ledger(Path(tmp) / "ledger.db") as ledger:
                ledger.create_batch("dry-1", "plan-1", "dry_run", None, now, {})
                self.assertIsNone(ledger.get_batch("latest"))
                self.assertEqual(ledger.get_batch("dry-1")["status"], "dry_run")
                ledger.create_batch("batch-1", "plan-1", "running", "/tmp/backup", now, {})
                ledger.finish_batch("batch-1", "completed", now, {"executed": 2})
                latest = ledger.get_batch("latest")
                self.assertEqual(latest["batch_id"], "batch-1")
                self.assertEqual(latest["status"], "completed")
                self.assertEqual(latest["summary"], {"executed": 2})
                self.assertEqual(len(ledger.list_batches()), 2)

    def test_append_and_filter_ledger_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            now = utc_now()
            with Ledger(Path(tmp) / "ledger.db") as ledger:
                ledger.append("batch_started", {"a": 1}, now, batch_id="b1")
                ledger.append("scan_completed", {"b": 2}, now, run_id="r1")
                ledger.append("batch_finished", {"c": 3}, now, batch_id="b1")
                events = ledger.get_ledger(batch_id="b1")
                self.assertEqual([item["event"] for item in events], ["batch_finished", "batch_started"])
                self.assertEqual(events[0]["payload"], {"c": 3})
                self.assertEqual(len(ledger.get_ledger(run_id="r1")), 1)
                self.assertEqual(len(ledger.get_ledger()), 3)

    def test_failed_run_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            now = utc_now()
            with Ledger(Path(tmp) / "ledger.db") as ledger:
                ledger.record_failed_run("scan-fail", "f" * 64, "/config.json", now, now,
                                         "broken manifest", "/primary", "/replica", "/work")
                run = ledger.get_run("scan-fail")
                self.assertEqual(run["status"], "failed")
                self.assertEqual(run["stats"], {"error": "broken manifest"})
                events = ledger.get_ledger(run_id="scan-fail")
            self.assertEqual(events[0]["event"], "scan_failed")


if __name__ == "__main__":
    unittest.main()
