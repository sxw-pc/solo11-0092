from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from lakemirror.provenance_chain import ProvenanceReleaseError
from lakemirror.provenance_evidence import ProvenanceEvidenceError
from lakemirror.provenance_receipts import verify_source_receipt, write_release_artifacts
from lakemirror.provenance_release import adjudicate_report_release

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "report_provenance"
MANIFEST = FIXTURE / "manifest.json"
TARGET_SHA = "8582601ab567a3fe22e06e8a7fa255e18d91a9cefa09bdf83d0be92bff0c8867"


def decisions(decision: dict[str, Any]) -> dict[str, Any]:
    return {item["contract_kind"]: item for item in decision["publication_decisions"]}


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "lakemirror", *args],
        cwd=str(ROOT), text=True, capture_output=True, timeout=120,
    )


class ReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "release"

    def copy_fixture(self, name: str = "") -> Path:
        root = Path(self.tmp.name) / (name or "provenance")
        shutil.copytree(FIXTURE / name if name else FIXTURE, root)
        return root

    def test_same_target_bytes_yield_two_distinct_contract_conclusions(self) -> None:
        decision = adjudicate_report_release(MANIFEST)
        self.assertTrue(decision["byte_reproducible"])
        self.assertEqual(decision["authorization_identity_basis"], "candidate_id")
        self.assertEqual(decision["target_sha256"], TARGET_SHA)
        by_kind = decisions(decision)
        reconstructed = by_kind["reproducible_authorized"]
        original = by_kind["origin_safe"]
        self.assertEqual(reconstructed["status"], "allowed")
        self.assertEqual(reconstructed["publication_kind"], "reconstructed_report_instance")
        self.assertEqual(original["status"], "blocked")
        self.assertEqual(original["reason_code"], "ORIGIN_NOT_PROVABLY_SAFE")
        self.assertEqual(reconstructed["publication_sha256"], TARGET_SHA)

    def test_reproducible_chain_avoids_greedy_forbidden_first_candidate(self) -> None:
        reconstructed = decisions(adjudicate_report_release(MANIFEST))["reproducible_authorized"]
        selected = reconstructed["selected_combination"]
        self.assertEqual(selected["source_ids"],
                         ["name-public", "value-public-b", "footer-public"])
        self.assertEqual(selected["forbidden_pair_hits"], [])
        self.assertTrue(selected["byte_equal"])
        self.assertEqual(selected["target_sha256"], TARGET_SHA)
        self.assertEqual(reconstructed["checks"], {
            "all_sources_external_allowed": True,
            "no_forbidden_pairs": True,
            "target_bytes_equal": True,
            "chain_complete": True,
        })
        self.assertNotIn("value-public-a", selected["source_ids"])

    def test_new_receipt_carries_every_source_identity_and_hashes(self) -> None:
        decision = adjudicate_report_release(MANIFEST)
        receipt = decisions(decision)["reproducible_authorized"]["source_receipt"]
        self.assertEqual(receipt["receipt_kind"], "reconstructed_report_instance")
        self.assertFalse(receipt["original_producer_receipt"])
        self.assertEqual(receipt["manifest_sha256"], decision["manifest_sha256"])
        self.assertEqual(receipt["target_sha256"], TARGET_SHA)
        self.assertEqual([s["candidate_id"] for s in receipt["chosen_sources"]],
                         ["name-public", "value-public-b", "footer-public"])
        self.assertEqual([s["use_class"] for s in receipt["chosen_sources"]],
                         ["external_allowed"] * 3)
        self.assertTrue(all(len(s["source_sha256"]) == 64
                            for s in receipt["chosen_sources"]))
        verified = verify_source_receipt(MANIFEST, receipt)
        self.assertTrue(verified["valid"])
        self.assertEqual(verified["reconstruction_sha256"], TARGET_SHA)

    def test_origin_safe_counterexamples_show_every_compatible_unsafe_chain(self) -> None:
        original = decisions(adjudicate_report_release(MANIFEST))["origin_safe"]
        ids = [entry["source_ids"] for entry in original["counterexamples"]]
        self.assertIn(["name-public", "value-public-a", "footer-public"], ids)
        self.assertIn(["name-public", "value-public-b", "footer-restricted"], ids)
        self.assertIn(["name-restricted", "value-restricted", "footer-restricted"], ids)
        self.assertEqual(original["combinations_checked"], 12)
        # Every byte-compatible origin except the single clean chain is unsafe, so
        # one clean reconstruction path cannot clear the unknown original producer.
        self.assertNotIn(["name-public", "value-public-b", "footer-public"], ids)
        self.assertEqual(len(ids), 11)
        self.assertTrue(all(entry["byte_equal"] for entry in original["counterexamples"]))
        self.assertTrue(all(entry["reconstruction_sha256"] == TARGET_SHA
                            for entry in original["counterexamples"]))
        self.assertEqual({reason["code"] for reason in original["reasons"]},
                         {"RESTRICTED_HISTORICAL_ORIGIN", "FORBIDDEN_HISTORICAL_PAIR"})
        self.assertIsNone(original["source_receipt"])

    def test_origin_safe_allows_original_only_when_all_history_is_clean(self) -> None:
        manifest = FIXTURE / "all_public" / "manifest.json"
        by_kind = decisions(adjudicate_report_release(manifest))
        self.assertEqual(by_kind["origin_safe"]["status"], "allowed")
        self.assertEqual(by_kind["origin_safe"]["publication_kind"],
                         "original_backfill_instance")
        self.assertEqual(by_kind["origin_safe"]["combinations_checked"], 8)
        self.assertTrue(by_kind["origin_safe"]["checks"]["all_compatible_origins_checked"])
        receipt = by_kind["origin_safe"]["source_receipt"]
        self.assertIsNone(receipt["original_producer_receipt"])
        self.assertTrue(verify_source_receipt(manifest, receipt)["valid"])

    def test_no_clean_chain_blocks_reconstruction_but_keeps_counterexamples(self) -> None:
        decision = adjudicate_report_release(FIXTURE / "restricted_blocked" / "manifest.json")
        by_kind = decisions(decision)
        blocked = by_kind["reproducible_authorized"]
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["reason_code"], "NO_AUTHORIZED_RECONSTRUCTION")
        self.assertIsNone(blocked["source_receipt"])
        self.assertEqual(by_kind["origin_safe"]["reason_code"], "ORIGIN_NOT_PROVABLY_SAFE")
        self.assertTrue(by_kind["origin_safe"]["counterexamples"])

    def test_same_digest_identity_substitution_is_rejected(self) -> None:
        receipt = copy.deepcopy(
            decisions(adjudicate_report_release(MANIFEST))
            ["reproducible_authorized"]["source_receipt"])
        receipt["chosen_sources"][1]["candidate_id"] = "value-restricted"
        with self.assertRaises(ProvenanceReleaseError):
            verify_source_receipt(MANIFEST, receipt)
        receipt = copy.deepcopy(
            decisions(adjudicate_report_release(MANIFEST))
            ["reproducible_authorized"]["source_receipt"])
        receipt["chosen_sources"][1] = {
            "chunk_id": "metric-value",
            "candidate_id": "value-public-a",
            "source_file": "chunk2-public-a.txt",
            "source_sha256": receipt["chosen_sources"][1]["source_sha256"],
            "use_class": "external_allowed",
        }
        with self.assertRaisesRegex(ProvenanceReleaseError, "forbidden"):
            verify_source_receipt(MANIFEST, receipt)
        receipt = copy.deepcopy(
            decisions(adjudicate_report_release(MANIFEST))
            ["reproducible_authorized"]["source_receipt"])
        receipt["chosen_sources"][1]["source_file"] = "chunk2-restricted.txt"
        with self.assertRaisesRegex(ProvenanceReleaseError, "substitution"):
            verify_source_receipt(MANIFEST, receipt)

    def test_receipt_misbinding_and_impersonation_are_rejected(self) -> None:
        receipt = decisions(adjudicate_report_release(MANIFEST))[
            "reproducible_authorized"]["source_receipt"]
        cases = [
            ("contract_kind", "origin_safe", "kind|contract|bound"),
            ("snapshot_id", "other-snapshot", "snapshot"),
            ("manifest_sha256", "0" * 64, "manifest"),
            ("target_sha256", "0" * 64, "target"),
            ("original_producer_receipt", True, "impersonate|producer"),
            ("receipt_kind", "original_backfill_instance", "kind"),
        ]
        for field, value, pattern in cases:
            with self.subTest(field=field):
                tampered = copy.deepcopy(receipt)
                tampered[field] = value
                with self.assertRaisesRegex(ProvenanceReleaseError, pattern):
                    verify_source_receipt(MANIFEST, tampered)
        incomplete = copy.deepcopy(receipt)
        incomplete["chosen_sources"] = incomplete["chosen_sources"][:2]
        with self.assertRaisesRegex(ProvenanceReleaseError, "incomplete"):
            verify_source_receipt(MANIFEST, incomplete)
        duplicated = copy.deepcopy(receipt)
        duplicated["chosen_sources"][2]["candidate_id"] = "name-public"
        with self.assertRaisesRegex(ProvenanceReleaseError, "duplicate"):
            verify_source_receipt(MANIFEST, duplicated)
        misbound_chunk = copy.deepcopy(receipt)
        misbound_chunk["chosen_sources"][0]["chunk_id"] = "metric-value"
        with self.assertRaisesRegex(ProvenanceReleaseError, "chunk"):
            verify_source_receipt(MANIFEST, misbound_chunk)

    def test_contract_selection_and_unbound_requests_are_rejected(self) -> None:
        only = adjudicate_report_release(MANIFEST, contract_kinds=["reproducible_authorized"])
        self.assertEqual([item["contract_kind"] for item in only["publication_decisions"]],
                         ["reproducible_authorized"])
        with self.assertRaisesRegex(ProvenanceReleaseError, "unsupported"):
            adjudicate_report_release(MANIFEST, contract_kinds=["unknown_contract"])
        with self.assertRaisesRegex(ProvenanceReleaseError, "empty"):
            adjudicate_report_release(MANIFEST, contract_kinds=[])
        with self.assertRaises(ProvenanceEvidenceError):
            adjudicate_report_release(MANIFEST / "missing.json")

    def test_writes_independent_reconstructed_instance_and_receipt(self) -> None:
        decision = adjudicate_report_release(MANIFEST)
        written = write_release_artifacts(MANIFEST, decision, self.out)
        names = {Path(path).name for path in written}
        self.assertEqual(names, {"report-reconstructed.txt",
                                 "reproducible_authorized.receipt.json"})
        self.assertEqual((self.out / "report-reconstructed.txt").read_bytes(),
                         (FIXTURE / "report.txt").read_bytes())
        receipt = json.loads(
            (self.out / "reproducible_authorized.receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["receipt_kind"], "reconstructed_report_instance")
        self.assertFalse(receipt["original_producer_receipt"])
        self.assertTrue(verify_source_receipt(MANIFEST, receipt)["valid"])

    def test_writes_original_instance_only_under_origin_safe_fixture(self) -> None:
        manifest = FIXTURE / "all_public" / "manifest.json"
        written = write_release_artifacts(
            manifest, adjudicate_report_release(manifest), self.out)
        names = {Path(path).name for path in written}
        self.assertEqual(names, {
            "report-reconstructed.txt", "report-original.txt",
            "reproducible_authorized.receipt.json", "origin_safe.receipt.json"})
        self.assertEqual((self.out / "report-original.txt").read_bytes(),
                         (FIXTURE / "all_public" / "report.txt").read_bytes())

    def test_write_rejects_decision_from_another_manifest(self) -> None:
        decision = adjudicate_report_release(MANIFEST)
        other = adjudicate_report_release(FIXTURE / "all_public" / "manifest.json")
        decision["manifest_sha256"] = other["manifest_sha256"]
        with self.assertRaisesRegex(ProvenanceReleaseError, "bound"):
            write_release_artifacts(MANIFEST, decision, self.out)


if __name__ == "__main__":
    unittest.main()
