"""Row-level business verdicts distinguish temporal evidence from file drift."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lakemirror.row_contract import evaluate_row_contract
from lakemirror.row_fixture import generate_row_fixture
from lakemirror.util import LakeMirrorError, sha256_file


def _bytes(root: Path) -> dict[str, str]:
    return {str(file.relative_to(root)): sha256_file(file) for file in root.rglob("*") if file.is_file()}


class RowContractTests(unittest.TestCase):
    def test_consistent_rows_are_consumable_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = generate_row_fixture("consistent", root)
            before = _bytes(root)
            verdict = evaluate_row_contract(root, fixture["contract"])
            self.assertEqual(verdict["consumer_status"], "ready")
            self.assertEqual(verdict["sides"]["replica"]["status"], "verified")
            self.assertEqual(before, _bytes(root))

    def test_late_arrival_changes_consumer_decision_without_file_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = generate_row_fixture("late_arrival", root)
            before = _bytes(root)
            early = evaluate_row_contract(root, fixture["contract"])
            later = evaluate_row_contract(root, fixture["contract"], "2024-03-01T13:00:00Z")
            self.assertEqual(early["consumer_status"], "pending")
            self.assertEqual(early["sides"]["replica"]["orders"][0]["reason"], "LATE_EVIDENCE_NOT_VISIBLE")
            self.assertEqual(later["consumer_status"], "ready")
            self.assertEqual(before, _bytes(root))

    def test_key_and_lifecycle_contradictions_are_not_pending(self) -> None:
        for scenario, code in (("orphan", "ORPHAN_EVENT"), ("invalid_timeline", "EVENT_BEFORE_ORDER"),
                               ("state_conflict", "ORDER_EVENT_STATE_MISMATCH"),
                               ("duplicate_order", "DUPLICATE_ORDER")):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                fixture = generate_row_fixture(scenario, root)
                result = evaluate_row_contract(root, fixture["contract"])
                self.assertEqual(result["consumer_status"], "blocked")
                replica = result["sides"]["replica"]
                reasons = {item["reason"] for item in replica["orders"]} | {item["code"] for item in replica["issues"]}
                self.assertIn(code, reasons)

    def test_corrupt_physical_evidence_is_unknown_not_business_inconsistent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = generate_row_fixture("corrupt_file", root)
            result = evaluate_row_contract(root, fixture["contract"])
            self.assertEqual(result["consumer_status"], "unknown")
            self.assertEqual(result["sides"]["replica"]["evidence_error"], "ROW_FILE_UNTRUSTED")

    def test_same_snapshot_id_with_different_active_manifest_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = generate_row_fixture("consistent", root)
            active = root / "lake" / "replica" / "tables" / "orders" / "_manifest.json"
            value = json.loads(active.read_text(encoding="utf-8"))
            value["stats"]["unexpected"] = 1
            active.write_text(json.dumps(value), encoding="utf-8")
            result = evaluate_row_contract(root, fixture["contract"])
            self.assertEqual(result["consumer_status"], "unknown")
            self.assertEqual(result["sides"]["replica"]["evidence_error"], "ROW_SNAPSHOT_UNSAFE")

    def test_contract_rejects_future_effective_cutoff_and_external_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = generate_row_fixture("consistent", root)
            with self.assertRaisesRegex(LakeMirrorError, "cannot exceed"):
                evaluate_row_contract(root, fixture["contract"], "2024-03-01T09:00:00Z")
            with self.assertRaisesRegex(LakeMirrorError, "inside workdir"):
                evaluate_row_contract(root, root.parent / "outside.json")

    def test_cli_fixture_and_as_of_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = subprocess.run([sys.executable, "-m", "lakemirror", "row-fixture", "--scenario", "late_arrival",
                                      "--workdir", temp], capture_output=True, text=True, check=True)
            contract = json.loads(fixture.stdout)["contract"]
            command = [sys.executable, "-m", "lakemirror", "row-contract", "--workdir", temp, "--contract", contract]
            early = subprocess.run(command, capture_output=True, text=True, check=True)
            later = subprocess.run(command + ["--as-of", "2024-03-01T13:00:00Z"],
                                   capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(early.stdout)["consumer_status"], "pending")
            self.assertEqual(json.loads(later.stdout)["consumer_status"], "ready")


if __name__ == "__main__":
    unittest.main()
