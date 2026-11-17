import unittest
from pathlib import Path
from lakemirror.fork_evidence import inspect_fork_contract


class ForkEvidenceTests(unittest.TestCase):
    def test_sibling_manifest_fork_and_disjoint_row_fields(self):
        root = Path(__file__).resolve().parents[1]
        result = inspect_fork_contract(root / "fixtures/fork_merge/contract.json")
        self.assertTrue(result["chain_forked"])
        self.assertIn("SNAPSHOT_FORK", [issue["code"] for issue in result["chain_issues"]])
        base = result["rows_by_snapshot"]["S0"]["O1"]
        left = result["rows_by_snapshot"]["S1"]["O1"]
        right = result["rows_by_snapshot"]["S2"]["O1"]
        self.assertEqual((base["amount"],left["amount"],right["amount"]),("100","120","100"))
        self.assertEqual((base["status"],left["status"],right["status"]),("hold","hold","approved"))
        self.assertIsNone(result["merge_verdict"])
        self.assertIsNone(result["merged_snapshot"])

    def test_delete_versus_update_conflict_fixture(self):
        root = Path(__file__).resolve().parents[1]
        result = inspect_fork_contract(root / "fixtures/fork_merge/conflict-contract.json")
        self.assertIn("O2", result["rows_by_snapshot"]["S0"])
        self.assertNotIn("O2", result["rows_by_snapshot"]["S1D"])
        self.assertEqual(result["rows_by_snapshot"]["S2U"]["O2"]["status"], "closed")
        self.assertTrue(result["chain_forked"])
        self.assertIsNone(result["merge_verdict"])
