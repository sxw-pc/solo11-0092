from __future__ import annotations

import unittest
from pathlib import Path

from lakemirror.privacy_evidence import PrivacyEvidenceError, inspect_privacy_table

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "privacy_table"


class PrivacyEvidenceTests(unittest.TestCase):
    def test_complete_table_margins_and_sensitive_cells_are_bound(self) -> None:
        result = inspect_privacy_table(FIXTURES / "contract.json")
        self.assertEqual(result["row_totals"], {"R1": 10, "R2": 10})
        self.assertEqual(result["col_totals"], {"C1": 7, "C2": 7, "C3": 6})
        self.assertEqual(result["grand_total"], 20)
        self.assertEqual([(x["row"], x["col"]) for x in result["cells"] if x["count"] < 2],
                         [("R1", "C1"), ("R2", "C3")])
        self.assertIsNone(result["feasible_sensitive_values"])
        self.assertIsNone(result["suppression_masks"])
        self.assertIsNone(result["publication_decisions"])
        self.assertEqual(result, inspect_privacy_table(FIXTURES / "contract.json"))
        corrected = inspect_privacy_table(FIXTURES / "corrected-contract.json")
        self.assertEqual(corrected["parent_snapshot_id"], result["snapshot_id"])
        self.assertEqual(corrected["row_totals"], {"R1": 13, "R2": 10})
        self.assertEqual(corrected["col_totals"], {"C1": 7, "C2": 10, "C3": 6})
        self.assertEqual(corrected["grand_total"], 23)

    def test_bad_table_sha_blocks(self) -> None:
        with self.assertRaisesRegex(PrivacyEvidenceError, "SHA differs"):
            inspect_privacy_table(FIXTURES / "bad-sha-contract.json")


if __name__ == "__main__":
    unittest.main()
