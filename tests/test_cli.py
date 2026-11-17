"""Command-line acceptance tests against temporary offline workspaces."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import config_data, write_config

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "lakemirror", *args],
        cwd=str(cwd or ROOT), text=True, capture_output=True, timeout=120,
    )


def tree_bytes(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)).replace("\\", "/"): path.read_bytes()
            for path in sorted(root.rglob("*")) if path.is_file()}


class CliAcceptanceTests(unittest.TestCase):
    def test_complete_scan_plan_execute_verify_rollback_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp).resolve() / "work"
            generated = run_cli("fixtures", "--scenario", "drift_basic", "--seed", "17", "--workdir", str(work))
            self.assertEqual(generated.returncode, 0, generated.stderr)
            self.assertEqual(json.loads(generated.stdout)["scenario"], "drift_basic")
            config_path = write_config(tmp, config_data(work), "config.json")

            scanned = run_cli("scan", "--config", str(config_path), "--workdir", str(work),
                              "--report", str(work / "scan.json"))
            self.assertEqual(scanned.returncode, 0, scanned.stderr)
            scan = json.loads(scanned.stdout)
            self.assertEqual(scan["stats"]["active_diffs"], 14)
            self.assertTrue(Path(scan["report"]).is_file())

            planned = run_cli("plan", "--scan-id", scan["scan_id"], "--workdir", str(work),
                              "--out", str(work / "plan.json"))
            self.assertEqual(planned.returncode, 0, planned.stderr)
            plan_info = json.loads(planned.stdout)
            self.assertEqual(plan_info["operation_count"], 11)
            self.assertEqual(plan_info["kind"], "executable")
            plan_path = Path(plan_info["out"])

            dry = run_cli("apply", "--plan", str(plan_path), "--workdir", str(work))
            self.assertEqual(dry.returncode, 0, dry.stderr)
            dry_result = json.loads(dry.stdout)
            self.assertEqual(dry_result["status"], "dry_run")
            self.assertEqual(dry_result["executed"], 11)
            self.assertIsNone(dry_result["backup_dir"])

            applied = run_cli("apply", "--plan", str(plan_path), "--workdir", str(work), "--execute")
            self.assertEqual(applied.returncode, 0, applied.stderr)
            applied_result = json.loads(applied.stdout)
            self.assertEqual(applied_result["status"], "completed")
            self.assertEqual(applied_result["executed"], 11)
            batch_id = applied_result["batch_id"]
            self.assertTrue(Path(applied_result["backup_dir"]).is_dir())

            verified = run_cli("verify", "--batch-id", batch_id, "--workdir", str(work))
            self.assertEqual(verified.returncode, 0, verified.stderr)
            self.assertEqual(json.loads(verified.stdout)["status"], "verified")

            rerun = run_cli("apply", "--plan", str(plan_path), "--workdir", str(work), "--execute")
            self.assertEqual(rerun.returncode, 0, rerun.stderr)
            rerun_result = json.loads(rerun.stdout)
            self.assertEqual(rerun_result["executed"], 0)
            self.assertEqual(rerun_result["skipped"], 11)

            rolled = run_cli("rollback", "--batch-id", batch_id, "--workdir", str(work), "--execute")
            self.assertEqual(rolled.returncode, 0, rolled.stderr)
            self.assertEqual(json.loads(rolled.stdout)["status"], "rolled_back")

            rescanned = run_cli("scan", "--config", str(config_path), "--workdir", str(work))
            self.assertEqual(rescanned.returncode, 0, rescanned.stderr)
            self.assertEqual(json.loads(rescanned.stdout)["stats"]["active_diffs"], 14)

            report = run_cli("report", "--workdir", str(work), "--format", "html",
                             "--out", str(work / "review.html"))
            self.assertEqual(report.returncode, 0, report.stderr)
            html = (work / "review.html").read_text(encoding="utf-8")
            self.assertIn("Lake Mirror reconciliation report", html)
            self.assertNotIn("https://", html)

    def test_report_renders_all_formats_to_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp).resolve() / "work"
            self.assertEqual(run_cli("fixtures", "--scenario", "clean_pair", "--workdir", str(work)).returncode, 0)
            config_path = write_config(tmp, config_data(work), "config.json")
            self.assertEqual(run_cli("scan", "--config", str(config_path), "--workdir", str(work)).returncode, 0)
            for fmt, marker in (("json", '"project": "lake-mirror"'), ("md", "# Lake Mirror reconciliation report"),
                                ("html", "<!doctype html>")):
                with self.subTest(fmt=fmt):
                    result = run_cli("report", "--workdir", str(work), "--format", fmt)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(marker, result.stdout)

    def test_fixture_generation_is_seed_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp).resolve() / "first"
            second = Path(tmp).resolve() / "second"
            third = Path(tmp).resolve() / "third"
            for work, seed in ((first, "42"), (second, "42"), (third, "43")):
                result = run_cli("fixtures", "--scenario", "drift_basic", "--seed", seed, "--workdir", str(work))
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(tree_bytes(first), tree_bytes(second))
            self.assertNotEqual(tree_bytes(first), tree_bytes(third))

    def test_clean_pair_yields_empty_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp).resolve() / "work"
            self.assertEqual(run_cli("fixtures", "--scenario", "clean_pair", "--workdir", str(work)).returncode, 0)
            config_path = write_config(tmp, config_data(work), "config.json")
            scan = json.loads(run_cli("scan", "--config", str(config_path), "--workdir", str(work)).stdout)
            plan = run_cli("plan", "--scan-id", scan["scan_id"], "--workdir", str(work),
                           "--out", str(work / "clean-plan.json"))
            self.assertEqual(plan.returncode, 0, plan.stderr)
            self.assertEqual(json.loads(plan.stdout)["operation_count"], 0)

    def test_workdir_mismatch_and_missing_scan_are_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp).resolve() / "work"
            self.assertEqual(run_cli("fixtures", "--scenario", "clean_pair", "--workdir", str(work)).returncode, 0)
            config_path = write_config(tmp, config_data(work), "config.json")
            mismatch = run_cli("scan", "--config", str(config_path), "--workdir", str(Path(tmp) / "other"))
            self.assertEqual(mismatch.returncode, 2)
            self.assertIn("WORKDIR_MISMATCH", mismatch.stderr)
            missing = run_cli("plan", "--scan-id", "scan-absent", "--workdir", str(work),
                              "--out", str(work / "never.json"))
            self.assertEqual(missing.returncode, 2)
            self.assertIn("SCAN_NOT_FOUND", missing.stderr)

    def test_missing_config_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_cli("scan", "--config", str(Path(tmp) / "missing.json"), "--workdir", tmp)
            self.assertEqual(result.returncode, 2)
            self.assertIn("error [", result.stderr)


if __name__ == "__main__":
    unittest.main()
