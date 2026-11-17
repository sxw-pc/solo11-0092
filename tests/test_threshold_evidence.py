import unittest
from pathlib import Path
from lakemirror.threshold_evidence import inspect_threshold_evidence


class ThresholdEvidenceTests(unittest.TestCase):
    def test_bounded_missing_refund_fixture(self):
        root = Path(__file__).resolve().parents[1]
        result = inspect_threshold_evidence(root / "fixtures/threshold_query/contract.json")
        self.assertEqual(result["known_net"], 185)
        self.assertEqual(result["naive_max_missing_refund"], 120)
        self.assertEqual(result["missing_files"], ["refunds-missing.csv"])
        self.assertEqual(result["query_decisions"], None)
        self.assertEqual(result["net_bounds"], None)
