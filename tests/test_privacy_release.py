"""Public-release decisions, leak regressions, and release whitelist tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lakemirror.privacy_evidence import (
    inspect_privacy_table,
    release_privacy_table,
    verify_privacy_release,
)
from lakemirror.privacy_preview import evaluate_candidate_mask, preview_primary_suppression
from lakemirror.privacy_release import (
    PUBLIC_KEYS,
    PrivacyReleaseError,
    build_privacy_release,
    verify_release_receipt,
    write_release_bundle,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "privacy_table"


def cell_counts(artifact: dict) -> dict[str, int]:
    return {f"{cell['row']}:{cell['col']}": cell["count"] for cell in artifact["visible_cells"]}


class ReleaseDecisionTests(unittest.TestCase):
    def test_exact_contract_needs_four_cells_and_hides_r1c3(self) -> None:
        release = release_privacy_table(FIXTURES / "contract.json", "exact_margins")
        public, receipt = release["public_artifact"], release["internal_receipt"]
        self.assertEqual(receipt["suppressed_cells"], ["R1:C1", "R1:C3", "R2:C1", "R2:C3"])
        self.assertEqual(receipt["optimality"]["minimum_suppressed_cells"], 4)
        self.assertEqual(receipt["optimality"]["evaluated_sizes"],
                         [{"suppressed_cells": 2, "protecting_masks": 0},
                          {"suppressed_cells": 3, "protecting_masks": 0},
                          {"suppressed_cells": 4, "protecting_masks": 1}])
        self.assertNotIn("R1:C3", cell_counts(public))
        self.assertEqual(cell_counts(public), {"R1:C2": 4, "R2:C2": 3})
        self.assertEqual(public["margins"]["row_totals"], {"R1": 10, "R2": 10})
        self.assertEqual(receipt["feasible_sensitive_values"],
                         {"R1:C1": list(range(7)), "R2:C3": list(range(7))})

    def test_rounded_contract_needs_two_cells_and_publishes_r1c3(self) -> None:
        release = release_privacy_table(FIXTURES / "contract.json", "rounded_margins")
        public, receipt = release["public_artifact"], release["internal_receipt"]
        self.assertEqual(receipt["suppressed_cells"], ["R1:C1", "R2:C3"])
        self.assertEqual(len(cell_counts(public)), 4)
        self.assertEqual(cell_counts(public), {"R1:C2": 4, "R1:C3": 5, "R2:C1": 6, "R2:C2": 3})
        self.assertEqual(public["margins"], {
            "row_totals": {"R1": 10, "R2": 10},
            "column_totals": {"C1": 5, "C2": 5, "C3": 5},
            "grand_total": 20,
        })
        self.assertEqual(receipt["feasible_sensitive_values"],
                         {"R1:C1": [0, 1], "R2:C3": [0, 1, 2]})

    def test_two_authorized_contracts_differ_only_on_r1c3_visibility(self) -> None:
        exact = release_privacy_table(FIXTURES / "contract.json", "exact_margins")
        rounded = release_privacy_table(FIXTURES / "contract.json", "rounded_margins")
        self.assertNotIn("R1:C3", cell_counts(exact["public_artifact"]))
        self.assertIn("R1:C3", cell_counts(rounded["public_artifact"]))
        self.assertNotEqual(exact["public_artifact"]["publication_id"],
                            rounded["public_artifact"]["publication_id"])

    def test_corrected_snapshot_requires_a_new_rounded_decision(self) -> None:
        base = release_privacy_table(FIXTURES / "contract.json", "rounded_margins")
        corrected = release_privacy_table(FIXTURES / "corrected-contract.json", "rounded_margins")
        self.assertEqual(base["internal_receipt"]["suppressed_cells"], ["R1:C1", "R2:C3"])
        self.assertEqual(corrected["internal_receipt"]["suppressed_cells"],
                         ["R1:C1", "R1:C2", "R2:C3"])
        self.assertEqual(corrected["internal_receipt"]["optimality"]["evaluated_sizes"],
                         [{"suppressed_cells": 2, "protecting_masks": 0},
                          {"suppressed_cells": 3, "protecting_masks": 3}])
        self.assertEqual(corrected["internal_receipt"]["feasible_sensitive_values"],
                         {"R1:C1": [0, 1], "R2:C3": [0, 1, 2]})
        self.assertNotEqual(base["public_artifact"]["publication_id"],
                            corrected["public_artifact"]["publication_id"])

    def test_corrected_exact_contract_remains_four_cells(self) -> None:
        release = release_privacy_table(FIXTURES / "corrected-contract.json", "exact_margins")
        self.assertEqual(release["internal_receipt"]["suppressed_cells"],
                         ["R1:C1", "R1:C3", "R2:C1", "R2:C3"])
        self.assertEqual(release["public_artifact"]["margins"]["row_totals"], {"R1": 13, "R2": 10})

    def test_selection_is_stable_for_one_input_and_contract(self) -> None:
        first = release_privacy_table(FIXTURES / "contract.json", "exact_margins")
        second = release_privacy_table(FIXTURES / "contract.json", "exact_margins")
        self.assertEqual(first, second)
        self.assertEqual(first["public_artifact"]["publication_id"],
                         second["public_artifact"]["publication_id"])


class PreviewLeakRegressionTests(unittest.TestCase):
    def test_exact_primary_preview_is_uniquely_recovered(self) -> None:
        evidence = inspect_privacy_table(FIXTURES / "contract.json")
        preview = preview_primary_suppression(evidence, "exact_margins")
        self.assertEqual(preview["candidate_values"], {"R1:C1": [1], "R2:C3": [1]})
        release = build_privacy_release(evidence, "exact_margins")
        self.assertNotEqual(release["internal_receipt"]["suppressed_cells"], preview["primary_mask"])
        self.assertNotIn("R1:C1", cell_counts(release["public_artifact"]))

    def test_rounded_primary_preview_is_not_a_release(self) -> None:
        evidence = inspect_privacy_table(FIXTURES / "contract.json")
        preview = preview_primary_suppression(evidence, "rounded_margins")
        self.assertFalse(preview["publishable"])
        self.assertFalse(preview.get("protects_sensitive_cells", False))
        self.assertTrue(preview["would_leak_if_source_sha_published"])
        release = build_privacy_release(evidence, "rounded_margins")
        self.assertEqual(release["internal_receipt"]["suppressed_cells"], preview["primary_mask"])
        self.assertTrue(release["public_artifact"]["publishable"])

    def test_corrected_reused_pair_fails_the_protection_rule(self) -> None:
        evidence = inspect_privacy_table(FIXTURES / "corrected-contract.json")
        candidate = evaluate_candidate_mask(evidence, "rounded_margins", ["R1:C1", "R2:C3"])
        self.assertFalse(candidate["protects_sensitive_cells"])
        release = build_privacy_release(evidence, "rounded_margins")
        self.assertEqual(len(release["internal_receipt"]["suppressed_cells"]), 3)


class PublicWhitelistTests(unittest.TestCase):
    def test_public_artifact_exposes_only_whitelisted_fields(self) -> None:
        for name in ("contract.json", "corrected-contract.json"):
            evidence = inspect_privacy_table(FIXTURES / name)
            for kind in ("exact_margins", "rounded_margins"):
                release = build_privacy_release(evidence, kind)
                public = release["public_artifact"]
                self.assertEqual(set(public), set(PUBLIC_KEYS))
                self.assertNotIn("cells_sha256", json.dumps(public))
                self.assertNotIn(evidence["cells_sha256"], json.dumps(public))
                self.assertNotIn(evidence["manifest_sha256"], json.dumps(public))
                self.assertNotIn('"suppressed_cells"', json.dumps(public))
                hidden = set(release["internal_receipt"]["suppressed_cells"])
                self.assertFalse(hidden & set(cell_counts(public)))
                self.assertNotIn("witnesses", public)
                self.assertNotIn("optimality", public)
                self.assertFalse(set(public) & {"cells", "source_cells_sha256", "true_cells"})

    def test_public_artifact_cannot_carry_internal_fields(self) -> None:
        evidence = inspect_privacy_table(FIXTURES / "contract.json")
        release = build_privacy_release(evidence, "rounded_margins")
        public = dict(release["public_artifact"])
        public["cells_sha256"] = evidence["cells_sha256"]
        with self.assertRaises(PrivacyReleaseError):
            verify_release_receipt(evidence, "rounded_margins", public,
                                   release["internal_receipt"])

    def test_visible_cells_are_true_and_not_candidates(self) -> None:
        evidence = inspect_privacy_table(FIXTURES / "contract.json")
        release = build_privacy_release(evidence, "rounded_margins")
        true_counts = {(cell["row"], cell["col"]): cell["count"] for cell in evidence["cells"]}
        for cell in release["public_artifact"]["visible_cells"]:
            self.assertEqual(true_counts[(cell["row"], cell["col"])], cell["count"])


class ReceiptBindingTests(unittest.TestCase):
    def test_receipt_witnesses_are_two_distinct_feasible_completions(self) -> None:
        evidence = inspect_privacy_table(FIXTURES / "contract.json")
        release = build_privacy_release(evidence, "rounded_margins")
        public = release["public_artifact"]
        receipt = release["internal_receipt"]
        visible = cell_counts(public)
        margins = public["margins"]
        for cell, options in receipt["feasible_sensitive_values"].items():
            self.assertGreaterEqual(len(options), 2)
            witnesses = receipt["witnesses"][cell]
            self.assertEqual(len(witnesses), 2)
            self.assertNotEqual(witnesses[0][cell], witnesses[1][cell])
            for witness in witnesses:
                for key, value in visible.items():
                    self.assertEqual(witness[key], value)
                for row, total in margins["row_totals"].items():
                    actual = sum(value for key, value in witness.items() if key.startswith(f"{row}:"))
                    self.assertLessEqual(abs(actual - total), 2)
                self.assertLessEqual(abs(sum(witness.values()) - margins["grand_total"]), 2)
                self.assertGreaterEqual(min(witness.values()), 0)
        for cell in receipt["sensitive_cells"]:
            self.assertIn(cell, receipt["witnesses"])

    def test_rounded_release_survives_an_independent_brute_force(self) -> None:
        from itertools import product

        evidence = inspect_privacy_table(FIXTURES / "contract.json")
        release = build_privacy_release(evidence, "rounded_margins")
        public = release["public_artifact"]
        true_cells = {(c["row"], c["col"]): c["count"] for c in evidence["cells"]}
        hidden = {tuple(key.split(":")) for key in release["internal_receipt"]["suppressed_cells"]}
        keys = [(row, col) for row in public["rows"] for col in public["columns"]]
        round_five = lambda value: ((value + 2) // 5) * 5
        feasible: dict[tuple[str, str], set[int]] = {key: set() for key in hidden}
        for values in product(range(8), repeat=len(hidden)):
            trial = {key: true_cells[key] for key in keys if key not in hidden}
            trial.update(zip(sorted(hidden), values))
            if any(round_five(sum(trial[row, col] for col in public["columns"]))
                   != public["margins"]["row_totals"][row] for row in public["rows"]):
                continue
            if any(round_five(sum(trial[row, col] for row in public["rows"]))
                   != public["margins"]["column_totals"][col] for col in public["columns"]):
                continue
            if round_five(sum(trial.values())) != public["margins"]["grand_total"]:
                continue
            for key in hidden:
                feasible[key].add(trial[key])
        for key, values in feasible.items():
            self.assertGreaterEqual(len(values), 2)
            self.assertIn(true_cells[key], values)

    def test_roundtrip_verify_accepts_authorized_pair(self) -> None:
        for name in ("contract.json", "corrected-contract.json"):
            for kind in ("exact_margins", "rounded_margins"):
                release = release_privacy_table(FIXTURES / name, kind)
                result = verify_privacy_release(FIXTURES / name, kind, release["public_artifact"],
                                                release["internal_receipt"])
                self.assertTrue(result["verified"])

    def test_stale_receipt_from_older_snapshot_is_rejected(self) -> None:
        stale = release_privacy_table(FIXTURES / "contract.json", "rounded_margins")
        with self.assertRaises(PrivacyReleaseError):
            verify_privacy_release(FIXTURES / "corrected-contract.json", "rounded_margins",
                                   stale["public_artifact"], stale["internal_receipt"])

    def test_contract_cross_reuse_is_rejected(self) -> None:
        exact = release_privacy_table(FIXTURES / "contract.json", "exact_margins")
        with self.assertRaises(PrivacyReleaseError):
            verify_privacy_release(FIXTURES / "contract.json", "rounded_margins",
                                   exact["public_artifact"], exact["internal_receipt"])
        receipt = dict(exact["internal_receipt"], contract_kind="rounded_margins")
        with self.assertRaises(PrivacyReleaseError):
            verify_privacy_release(FIXTURES / "contract.json", "rounded_margins",
                                   exact["public_artifact"], receipt)

    def test_tampered_public_cells_are_rejected(self) -> None:
        release = release_privacy_table(FIXTURES / "contract.json", "rounded_margins")
        public = json.loads(json.dumps(release["public_artifact"]))
        public["visible_cells"][0]["count"] += 1
        with self.assertRaises(PrivacyReleaseError):
            verify_release_receipt(inspect_privacy_table(FIXTURES / "contract.json"),
                                   "rounded_margins", public, release["internal_receipt"])

    def test_bundle_writer_splits_public_and_internal_files(self) -> None:
        release = release_privacy_table(FIXTURES / "contract.json", "rounded_margins")
        with tempfile.TemporaryDirectory() as tmp:
            written = write_release_bundle(release, tmp)
            self.assertEqual(set(written), {"public_artifact", "internal_receipt"})
            public = Path(written["public_artifact"]).read_text(encoding="utf-8")
            receipt = json.loads(Path(written["internal_receipt"]).read_text(encoding="utf-8"))
            self.assertNotIn(release["internal_receipt"]["source_cells_sha256"], public)
            self.assertIn("suppressed_cells", receipt)


if __name__ == "__main__":
    unittest.main()
