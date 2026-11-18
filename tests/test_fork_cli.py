"""CLI and byte-rendering tests for the dual-parent fork merge workflow."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lakemirror.fork_merge import render_csv

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "fork_merge"


class ForkCliTests(unittest.TestCase):
    def test_cli_fork_merge_blocks_and_publishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = str(Path(tmp) / "sandbox")
            blocked = subprocess.run(
                [sys.executable, "-m", "lakemirror", "fork-merge",
                 "--contract", str(FIXTURES / "contract.json"),
                 "--contract-name", "file_strict", "--sandbox", sandbox, "--execute"],
                cwd=ROOT, capture_output=True, text=True,
            )
            self.assertEqual(blocked.returncode, 2)
            self.assertIn("FILE_STRICT_BOTH_SIDES_CHANGED", blocked.stdout)
            published = subprocess.run(
                [sys.executable, "-m", "lakemirror", "fork-merge",
                 "--contract", str(FIXTURES / "contract.json"),
                 "--contract-name", "field_disjoint", "--sandbox", sandbox, "--execute"],
                cwd=ROOT, capture_output=True, text=True,
            )
            self.assertEqual(published.returncode, 0, published.stderr)
            payload = json.loads(published.stdout)
            self.assertEqual(payload["status"], "published")
            self.assertEqual(payload["parents"], ["S1", "S2"])

    def test_cli_fork_evidence_reports_both_contracts(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "lakemirror", "fork-evidence",
             "--contract", str(FIXTURES / "contract.json")],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["chain_forked"])
        self.assertFalse(payload["merge_verdicts"]["file_strict"]["allowed"])
        self.assertTrue(payload["merge_verdicts"]["field_disjoint"]["allowed"])


class RenderCsvTests(unittest.TestCase):
    def test_render_is_byte_deterministic(self) -> None:
        headers = ["order_id", "amount", "status"]
        rows = [{"order_id": "O1", "amount": "120", "status": "approved"}]
        first = render_csv(headers, rows)
        self.assertEqual(first, render_csv(headers, rows))
        self.assertTrue(first.endswith(b"\n"))


if __name__ == "__main__":
    unittest.main()
