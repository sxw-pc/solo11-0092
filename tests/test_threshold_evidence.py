"""Bounded exact/threshold query contracts over verified and missing file bytes."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from lakemirror.threshold_evidence import inspect_threshold_evidence, query_threshold_evidence
from lakemirror.util import LakeMirrorError

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures/threshold_query"
CONTRACT = FIXTURE / "contract.json"


def run(contract: Path) -> dict:
    return query_threshold_evidence(contract)


class ThresholdEvidenceTests(unittest.TestCase):
    def test_bounded_missing_refund_fixture(self) -> None:
        result = run(CONTRACT)
        self.assertEqual(result["known_net"], 185)
        self.assertEqual(result["naive_max_missing_refund"], 120)
        self.assertEqual(result["max_feasible_missing_refund"], 90)
        self.assertEqual(result["missing_files"], ["refunds-missing.csv"])
        self.assertEqual([slot["slot"] for slot in result["missing_slots"]], ["R1", "R2"])
        self.assertEqual(result["net_bounds"]["lower"], 95)
        self.assertEqual(result["net_bounds"]["upper"], 185)
        self.assertIsNone(result["net_bounds"]["exact"])
        self.assertEqual(result["threshold"], 90)

    def test_contracts_are_independent_and_do_not_override_each_other(self) -> None:
        result = run(CONTRACT)
        exact = result["query_decisions"]["exact_result"]
        proof = result["query_decisions"]["threshold_proof"]
        self.assertEqual(exact["decision"], "unknown")
        self.assertFalse(exact["consumption_allowed"])
        self.assertIsNone(exact["net"])
        self.assertEqual(proof["decision"], "true")
        self.assertTrue(proof["consumption_allowed"])
        self.assertEqual(proof["predicate"], "net_revenue >= 90")
        self.assertEqual(proof["reason"], "ALL_LEGAL_COMPLETIONS_MEET_THRESHOLD")
        self.assertNotIn("net", proof)
        self.assertNotIn("exact", proof)

    def test_worst_case_witness_respects_slot_competition_and_order_limits(self) -> None:
        result = run(CONTRACT)
        witness = result["worst_case_completion"]
        self.assertEqual(
            [(item["slot"], item["order_id"], item["amount"]) for item in witness],
            [("R1", "O2", 60), ("R2", "O3", 30)],
        )
        orders = [item["order_id"] for item in witness]
        self.assertEqual(len(orders), len(set(orders)))
        self.assertEqual(result["query_decisions"]["threshold_proof"]["worst_case_completion"], witness)

    def test_naive_independent_caps_alone_would_not_prove_the_threshold(self) -> None:
        result = run(CONTRACT)
        self.assertEqual(result["known_net"] - result["naive_max_missing_refund"], 65)
        self.assertGreaterEqual(result["known_net"] - result["max_feasible_missing_refund"], result["threshold"])

    def test_verified_files_bind_manifest_and_snapshot_identity(self) -> None:
        result = run(CONTRACT)
        digests = {item["path"]: item["sha256"] for item in result["verified_files"]}
        self.assertEqual(digests["orders.csv"],
                         "d426ab7a8401c10879004bb72d3e7ac04151b6b2f2ca1d9bb3df0dfc6e960e5d")
        self.assertEqual(digests["refunds-known.csv"],
                         "c491de90d11f269dcf062428bd32050aa94ea0e18916ae92c12aee68ac1441a4")
        self.assertEqual(result["snapshot_id"], "snap-threshold-0001")
        self.assertEqual(len(result["manifests"]["orders"]["sha256"]), 64)
        self.assertEqual(len(result["manifests"]["refunds"]["sha256"]), 64)
        identity = result["missing_file_identities"][0]
        self.assertEqual((identity["path"], identity["row_count"], identity["slot_count"]),
                         ("refunds-missing.csv", 2, 2))
        self.assertFalse(identity["bytes_present"])

    def test_repeated_runs_are_byte_identical(self) -> None:
        first = json.dumps(inspect_threshold_evidence(CONTRACT), sort_keys=True)
        second = json.dumps(inspect_threshold_evidence(CONTRACT), sort_keys=True)
        self.assertEqual(first, second)

    def test_complete_input_yields_exact_and_rechecks_bounds(self) -> None:
        result = run(FIXTURE / "contract-complete.json")
        exact = result["query_decisions"]["exact_result"]
        proof = result["query_decisions"]["threshold_proof"]
        self.assertEqual(result["missing_files"], [])
        self.assertEqual(exact["decision"], "exact")
        self.assertEqual(exact["net"], 185)
        self.assertTrue(exact["within_computed_bounds"])
        self.assertEqual(result["net_bounds"], {"lower": 185, "upper": 185, "exact": 185,
                                                "lower_witness": [], "upper_basis": "verified_rows"})
        self.assertEqual(proof["decision"], "true")
        self.assertEqual(proof["reason"], "EXACT_VALUE_MEETS_THRESHOLD")

    def test_undecidable_threshold_is_unknown_not_true(self) -> None:
        result = run(FIXTURE / "contract-undecidable.json")
        proof = result["query_decisions"]["threshold_proof"]
        self.assertEqual(proof["decision"], "unknown")
        self.assertFalse(proof["consumption_allowed"])
        self.assertEqual(proof["reason"], "THRESHOLD_NOT_DECIDABLE")
        self.assertLess(result["net_bounds"]["lower"], result["threshold"])
        self.assertGreaterEqual(result["net_bounds"]["upper"], result["threshold"])

    def test_refuted_threshold_is_false(self) -> None:
        result = run(FIXTURE / "contract-refuted.json")
        proof = result["query_decisions"]["threshold_proof"]
        self.assertEqual(proof["decision"], "false")
        self.assertFalse(proof["consumption_allowed"])
        self.assertEqual(proof["reason"], "ALL_LEGAL_COMPLETIONS_BELOW_THRESHOLD")

    def test_unknown_candidate_order_is_rejected(self) -> None:
        with self.assertRaises(LakeMirrorError):
            run(FIXTURE / "contract-untrusted-candidate.json")

    def test_duplicate_refund_identity_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = _copy_fixture(tmp)
            _write_csv(target / "refunds-known.csv", "refund_id,order_id,amount\nR1,O1,25\n")
            with self.assertRaises(LakeMirrorError):
                run(target / "contract.json")

    def test_negative_amount_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = _copy_fixture(tmp)
            _write_csv(target / "refunds-known.csv", "refund_id,order_id,amount\nR0,O1,-25\n")
            with self.assertRaises(LakeMirrorError):
                run(target / "contract.json")

    def test_verified_byte_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = _copy_fixture(tmp)
            path = target / "refunds-known.csv"
            path.write_bytes(path.read_bytes().replace(b"25", b"24"))
            with self.assertRaises(LakeMirrorError):
                run(target / "contract.json")

    def test_manifest_row_count_change_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = _copy_fixture(tmp)
            _update_json(target / "orders-manifest.json",
                         lambda data: data["files"][0].update({"row_count": 5}))
            with self.assertRaises(LakeMirrorError):
                run(target / "contract.json")

    def test_modified_manifest_values_causally_change_the_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = _copy_fixture(tmp)
            _update_json(target / "refunds-manifest.json",
                         lambda data: data["stats"]["missing_slots"][0].update({"max_amount": 30}))
            result = run(target / "contract.json")
            self.assertEqual(result["max_feasible_missing_refund"], 70)
            self.assertEqual(result["net_bounds"]["lower"], 115)
            self.assertEqual(result["query_decisions"]["threshold_proof"]["decision"], "true")

            _update_json(target / "refunds-manifest.json",
                         lambda data: data["stats"]["missing_slots"][0].update({"max_amount": 120}))
            weaker = run(target / "contract.json")
            self.assertEqual(weaker["max_feasible_missing_refund"], 100)
            self.assertEqual(weaker["net_bounds"]["lower"], 85)
            self.assertEqual(weaker["query_decisions"]["threshold_proof"]["decision"], "unknown")
            self.assertEqual(weaker["query_decisions"]["exact_result"]["decision"], "unknown")

    def test_snapshot_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = _copy_fixture(tmp)
            _update_json(target / "refunds-manifest.json",
                         lambda data: data.update({"snapshot_id": "snap-other"}))
            with self.assertRaises(LakeMirrorError):
                run(target / "contract.json")

    def test_missing_slot_without_missing_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = _copy_fixture(tmp)
            _update_json(target / "refunds-manifest.json",
                         lambda data: data["files"].pop(1))
            with self.assertRaises(LakeMirrorError):
                run(target / "contract.json")

    def test_missing_file_without_slots_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = _copy_fixture(tmp)
            _update_json(target / "refunds-manifest.json",
                         lambda data: data["stats"].update({"missing_slots": []}))
            with self.assertRaises(LakeMirrorError):
                run(target / "contract.json")

    def test_constraint_flags_must_be_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = _copy_fixture(tmp)
            _update_json(target / "refunds-manifest.json",
                         lambda data: data["stats"].update({"one_refund_per_order": False}))
            with self.assertRaises(LakeMirrorError):
                run(target / "contract.json")

    def test_excess_refund_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = _copy_fixture(tmp)
            _write_csv(target / "refunds-known.csv", "refund_id,order_id,amount\nR0,O4,25\n")
            with self.assertRaises(LakeMirrorError):
                run(target / "contract.json")

    def test_missing_refund_bytes_are_never_read_from_elsewhere(self) -> None:
        # Present bytes for a missing entry must match the trusted manifest exactly,
        # otherwise the query refuses instead of accepting substitute bytes.
        with tempfile.TemporaryDirectory() as tmp:
            target = _copy_fixture(tmp)
            _write_csv(target / "refunds-missing.csv", "refund_id,order_id,amount\nR1,O2,60\nR2,O3,30\n")
            with self.assertRaises(LakeMirrorError):
                run(target / "contract.json")
            # Trusting those bytes explicitly is a different, verified input.
            _update_json(target / "refunds-manifest.json",
                         lambda data: data["files"][1].update({
                             "size": (target / "refunds-missing.csv").stat().st_size,
                             "sha256": _sha(target / "refunds-missing.csv"),
                         }))
            _update_json(target / "refunds-manifest.json",
                         lambda data: data["stats"].update({"missing_slots": []}))
            result = run(target / "contract.json")
            self.assertEqual(result["missing_files"], [])
            self.assertEqual(result["net_bounds"]["exact"], 95)
            self.assertEqual(result["query_decisions"]["exact_result"]["net"], 95)


def _copy_fixture(tmp: str) -> Path:
    target = Path(tmp).resolve() / "fixtures/threshold_query"
    target.mkdir(parents=True)
    for entry in FIXTURE.iterdir():
        if entry.is_file():
            shutil.copy2(entry, target / entry.name)
    return target


def _write_csv(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def _update_json(path: Path, mutate) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _sha(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()
