"""Report assembly and golden JSON/Markdown/HTML rendering tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lakemirror.ledger import Ledger
from lakemirror.planner import build_plan
from lakemirror.reconcile import scan_config
from lakemirror.report import (
    BOUNDARIES,
    ReportError,
    build_report,
    render_json,
    render_markdown,
    render_report,
    write_report,
)
from lakemirror.util import utc_now
from tests.support import make_config

GOLDEN = Path(__file__).resolve().parent / "golden"


def sample_report() -> dict:
    return {
        "schema_version": 1,
        "project": "lake-mirror",
        "run": {
            "run_id": "scan-demo",
            "status": "completed",
            "started_at": "2024-04-01T00:00:00Z",
            "finished_at": "2024-04-01T00:00:02Z",
            "config_fingerprint": "a" * 64,
            "config_path": "/sandbox/work/config.json",
            "primary_root": "/sandbox/work/lake/primary",
            "replica_root": "/sandbox/work/lake/replica",
        },
        "summary": {
            "tables": 1,
            "total_diffs": 3,
            "active_diffs": 2,
            "auto_fixable_diffs": 1,
            "manual_review_diffs": 1,
            "ignored_count": 1,
            "counts_by_code": {
                "FILE_EXTRA_IN_REPLICA": 1,
                "FILE_MISSING_IN_REPLICA": 1,
                "SCHEMA_TYPE_MISMATCH": 1,
            },
            "counts_by_severity": {"high": 2, "medium": 1},
        },
        "tables": {
            "orders": {
                "primary_files": 2, "replica_files": 2, "primary_bytes": 120,
                "replica_bytes": 115, "primary_snapshot": "snap-orders-0002",
                "replica_snapshot": "snap-orders-0002", "business_key": "order_id",
                "partition_columns": ["dt"],
            }
        },
        "ignore_rules": [
            {"code": "FILE_EXTRA_IN_REPLICA", "table": "orders",
             "path_prefix": "data/dt=2024-01-02/part-9999.csv", "reason": "owner is investigating"},
        ],
        "diffs": [
            {
                "diff_id": "diff-111111111111-1", "run_id": "scan-demo",
                "code": "FILE_MISSING_IN_REPLICA", "table": "orders", "partition": "dt=2024-01-01",
                "path": "data/dt=2024-01-01/part-0001.csv",
                "expected": {"size": 42, "sha256": "b" * 64}, "actual": None,
                "severity": "high", "suggestion": "Copy the verified primary file into the replica.",
                "auto_fixable": True, "ignored": False, "ignore_reason": None,
                "evidence": {"logical_key": "dt=2024-01-01|data/dt=2024-01-01/part-0001.csv"},
            },
            {
                "diff_id": "diff-222222222222-1", "run_id": "scan-demo",
                "code": "SCHEMA_TYPE_MISMATCH", "table": "orders", "partition": "", "path": "amount",
                "expected": "DECIMAL", "actual": "STRING", "severity": "high",
                "suggestion": "Schema drift is blocking and needs owner review.",
                "auto_fixable": False, "ignored": False, "ignore_reason": None,
                "evidence": {"message": "field amount type differs", "blocking": True},
            },
            {
                "diff_id": "diff-333333333333-1", "run_id": "scan-demo",
                "code": "FILE_EXTRA_IN_REPLICA", "table": "orders", "partition": "dt=2024-01-02",
                "path": "data/dt=2024-01-02/part-9999.csv",
                "expected": None, "actual": {"size": 5, "sha256": "d" * 64}, "severity": "medium",
                "suggestion": "Delete the unreferenced replica file after backup.",
                "auto_fixable": False, "ignored": True, "ignore_reason": "owner is investigating",
                "evidence": {"logical_key": "dt=2024-01-02|data/dt=2024-01-02/part-9999.csv"},
            },
        ],
        "snapshots": [
            {"run_id": "scan-demo", "side": "primary", "table_name": "orders",
             "snapshot_id": "snap-orders-0001", "parent_snapshot_id": None, "sequence": 1,
             "created_at": "2024-03-01T00:00:00Z", "source": "primary", "file_count": 2,
             "total_bytes": 120, "issues": []},
            {"run_id": "scan-demo", "side": "replica", "table_name": "orders",
             "snapshot_id": "snap-orders-0002", "parent_snapshot_id": "snap-orders-0001",
             "sequence": 2, "created_at": "2024-03-02T00:00:00Z", "source": "replica",
             "file_count": 2, "total_bytes": 115, "issues": []},
        ],
        "plans": [
            {
                "schema_version": 1, "plan_id": "plan-cccccccccccccccc",
                "plan_fingerprint": "c" * 64, "kind": "executable", "executable": True,
                "scan_id": "scan-demo", "config_fingerprint": "a" * 64,
                "config_path": "/sandbox/work/config.json", "workdir": "/sandbox/work",
                "operation_count": 2, "manual_review_count": 1,
                "operations": [
                    {
                        "type": "copy_file", "table": "orders", "partition": "dt=2024-01-01",
                        "path": "data/dt=2024-01-01/part-0001.csv",
                        "source_rel": "data/dt=2024-01-01/part-0001.csv",
                        "target_rel": "data/dt=2024-01-01/part-0001.csv",
                        "preconditions": ["plan fingerprint matches"], "expected": {"size": 42},
                        "impact": {"table": "orders", "files": 1, "bytes": 42, "destructive": False},
                        "auto_executable": True, "rollback": "remove the copied file",
                        "diff_codes": ["FILE_MISSING_IN_REPLICA"], "op_id": "op-0001-abcdef12",
                    },
                    {
                        "type": "manual_review", "table": "orders", "partition": "",
                        "path": "amount", "source_rel": None, "target_rel": None,
                        "preconditions": ["human owner confirms the expected state"],
                        "expected": "DECIMAL", "impact": {"table": "orders", "blocking": True},
                        "auto_executable": False, "rollback": "none",
                        "reason": "Schema drift is blocking and needs owner review.",
                        "diff_codes": ["SCHEMA_TYPE_MISMATCH"], "op_id": "op-0002-fedcba98",
                    },
                ],
            }
        ],
        "batches": [
            {"batch_id": "batch-1", "plan_id": "plan-cccccccccccccccc", "status": "completed",
             "backup_dir": "/sandbox/work/backups/batch-1", "started_at": "2024-04-01T00:01:00Z",
             "finished_at": "2024-04-01T00:01:02Z", "summary": {"executed": 1, "failed": 0}},
        ],
        "ledger": [
            {"event_id": 1, "run_id": "scan-demo", "batch_id": None, "event": "scan_completed",
             "payload_json": '{"diff_count":3}', "payload": {"diff_count": 3},
             "created_at": "2024-04-01T00:00:02Z"},
            {"event_id": 2, "run_id": None, "batch_id": "batch-1", "event": "batch_finished",
             "payload_json": '{"executed":1}', "payload": {"executed": 1},
             "created_at": "2024-04-01T00:01:02Z"},
        ],
        "boundaries": BOUNDARIES,
    }


class GoldenRenderTests(unittest.TestCase):
    def test_json_matches_golden(self) -> None:
        actual = render_report(sample_report(), "json")
        self.assertEqual(actual, (GOLDEN / "report.json").read_text(encoding="utf-8"))

    def test_markdown_matches_golden(self) -> None:
        actual = render_report(sample_report(), "md")
        self.assertEqual(actual, (GOLDEN / "report.md").read_text(encoding="utf-8"))

    def test_html_matches_golden(self) -> None:
        actual = render_report(sample_report(), "html")
        self.assertEqual(actual, (GOLDEN / "report.html").read_text(encoding="utf-8"))

    def test_unknown_format_is_rejected(self) -> None:
        with self.assertRaises(ReportError) as caught:
            render_report(sample_report(), "pdf")
        self.assertEqual(caught.exception.code, "REPORT_FORMAT")

    def test_html_is_self_contained_and_escaped(self) -> None:
        report = sample_report()
        report["diffs"][0]["path"] = 'data/<script>alert("x")</script>.csv'
        html = render_report(report, "html")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertIn("default-src 'none'", html)
        self.assertIn("no external assets", html)
        self.assertEqual(render_report(report, "html"), render_report(report, "html"))

    def test_json_is_deterministic_and_parseable(self) -> None:
        first = render_json(sample_report())
        second = render_json(sample_report())
        self.assertEqual(first, second)
        value = json.loads(first)
        self.assertEqual(value["run"]["run_id"], "scan-demo")
        self.assertEqual(value["summary"]["active_diffs"], 2)


class BuildReportTests(unittest.TestCase):
    def test_report_joins_scan_plan_batch_and_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            result = scan_config(config, "scan-report")
            plan = build_plan(config, result.scan_id, [item.as_dict() for item in result.diffs])
            now = "2024-04-01T00:00:00Z"
            with Ledger(Path(tmp) / "ledger.db") as ledger:
                ledger.save_scan(result, now, now)
                ledger.save_plan(plan, now)
                ledger.create_batch("batch-report", plan["plan_id"], "running", str(Path(config.workdir) / "backups" / "batch-report"), now, {})
                ledger.finish_batch("batch-report", "completed", now, {"executed": 11})
                ledger.append("batch_finished", {"executed": 11}, now, batch_id="batch-report")
                report = build_report(ledger, "scan-report")
                latest = build_report(ledger, "latest")
            self.assertEqual(report["run"]["run_id"], "scan-report")
            self.assertEqual(report["summary"]["active_diffs"], 14)
            self.assertEqual(report["summary"]["tables"], 2)
            self.assertEqual(len(report["diffs"]), 14)
            self.assertEqual(len(report["snapshots"]), 8)
            self.assertEqual(report["plans"][0]["plan_id"], plan["plan_id"])
            self.assertEqual(report["batches"][0]["batch_id"], "batch-report")
            events = {item["event"] for item in report["ledger"]}
            self.assertIn("scan_completed", events)
            self.assertIn("batch_finished", events)
            self.assertEqual(report["boundaries"], BOUNDARIES)
            self.assertEqual(latest["run"]["run_id"], "scan-report")
            self.assertEqual(report["ignore_rules"], [])

    def test_report_includes_ignore_rules_and_audited_diffs(self) -> None:
        rule = {"code": "FILE_EXTRA_IN_REPLICA", "table": "events",
                "path_prefix": "data/day=2024-02-09", "reason": "audited"}
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, ignore_rules=[rule])
            result = scan_config(config, "scan-ignored")
            now = utc_now()
            with Ledger(Path(tmp) / "ledger.db") as ledger:
                ledger.save_scan(result, now, now)
                report = build_report(ledger, "scan-ignored")
            self.assertEqual(report["ignore_rules"][0]["reason"], "audited")
            ignored = [item for item in report["diffs"] if item["ignored"]]
            self.assertEqual(len(ignored), 1)
            markdown = render_markdown(report)
            self.assertIn("Ignored conflicts (audited, not repaired)", markdown)
            self.assertIn("audited", markdown)

    def test_missing_run_raises_report_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with Ledger(Path(tmp) / "ledger.db") as ledger:
                with self.assertRaises(ReportError) as caught:
                    build_report(ledger, "scan-missing")
            self.assertEqual(caught.exception.code, "REPORT_RUN_MISSING")

    def test_write_report_creates_parent_and_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            result = scan_config(config, "scan-write")
            now = utc_now()
            with Ledger(Path(tmp) / "ledger.db") as ledger:
                ledger.save_scan(result, now, now)
                path = write_report(ledger, "scan-write", "json", Path(tmp) / "nested" / "report.json")
            self.assertTrue(path.is_file())
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["run"]["run_id"], "scan-write")


if __name__ == "__main__":
    unittest.main()
