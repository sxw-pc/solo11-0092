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


def decisions(decision: dict[str, Any]) -> dict[str, Any]:
    return {item["contract_kind"]: item for item in decision["publication_decisions"]}


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "lakemirror", *args],
        cwd=str(ROOT), text=True, capture_output=True, timeout=120,
    )


class ReceiptIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "release"

    def copy_fixture(self) -> Path:
        root = Path(self.tmp.name) / "provenance"
        shutil.copytree(FIXTURE, root)
        return root

    def test_forged_origin_safe_allow_decision_still_refuses_to_write(self) -> None:
        decision = adjudicate_report_release(MANIFEST)
        blocked = decisions(decision)["origin_safe"]
        blocked["status"] = "allowed"
        blocked["publication_kind"] = "original_backfill_instance"
        blocked["source_receipt"] = {
            "receipt_kind": "original_backfill_instance",
            "contract_kind": "origin_safe",
            "snapshot_id": decision["snapshot_id"],
            "manifest_sha256": decision["manifest_sha256"],
            "target_file": decision["target_file"],
            "target_sha256": decision["target_sha256"],
            "original_producer_receipt": None,
        }
        with self.assertRaisesRegex(ProvenanceReleaseError, "origin-safe"):
            write_release_artifacts(MANIFEST, decision, self.out)
        self.assertFalse((self.out / "report-original.txt").exists())

    def test_reproducible_receipt_rejects_wrong_allowed_instance_kind(self) -> None:
        decision = adjudicate_report_release(MANIFEST)
        item = decisions(decision)["reproducible_authorized"]
        item["publication_kind"] = "original_backfill_instance"
        with self.assertRaisesRegex(ProvenanceReleaseError, "kind"):
            write_release_artifacts(MANIFEST, decision, self.out)
        self.assertFalse((self.out / "report-original.txt").exists())

    def test_cross_contract_receipt_cannot_launder_an_original_instance(self) -> None:
        decision = adjudicate_report_release(MANIFEST)
        by_kind = decisions(decision)
        original = by_kind["origin_safe"]
        original["status"] = "allowed"
        original["publication_kind"] = "original_backfill_instance"
        original["source_receipt"] = copy.deepcopy(
            by_kind["reproducible_authorized"]["source_receipt"])
        with self.assertRaisesRegex(ProvenanceReleaseError, "contract"):
            write_release_artifacts(MANIFEST, decision, self.out)
        self.assertFalse((self.out / "report-original.txt").exists())

    def test_unbound_contract_kind_cannot_escape_output_directory(self) -> None:
        decision = adjudicate_report_release(MANIFEST)
        item = decision["publication_decisions"][0]
        item["contract_kind"] = "../../escaped"
        item["publication_kind"] = "reconstructed_report_instance"
        with self.assertRaises(ProvenanceReleaseError):
            write_release_artifacts(MANIFEST, decision, self.out)
        self.assertFalse((Path(self.tmp.name) / "escaped.receipt.json").exists())

    def test_receipt_evidence_fields_must_match_recomputed_chain(self) -> None:
        receipt = decisions(adjudicate_report_release(MANIFEST))[
            "reproducible_authorized"]["source_receipt"]
        cases = [
            ("byte_equal", False),
            ("reconstruction_sha256", "0" * 64),
            ("forbidden_pair_hits", [["name-public", "value-public-a"]]),
            ("forbidden_pairs_checked", []),
        ]
        for field, value in cases:
            with self.subTest(field=field):
                tampered = copy.deepcopy(receipt)
                tampered[field] = value
                with self.assertRaises(ProvenanceReleaseError):
                    verify_source_receipt(MANIFEST, tampered)

    def test_original_receipt_must_carry_multi_chain_evidence(self) -> None:
        manifest = FIXTURE / "all_public" / "manifest.json"
        receipt = decisions(adjudicate_report_release(manifest))[
            "origin_safe"]["source_receipt"]
        self.assertTrue(receipt["byte_equal"])
        self.assertEqual(receipt["forbidden_pairs_checked"], [])
        for field, value in (("combinations_checked", 1), ("byte_equal", False),
                             ("source_ids", ["value-public-a"])):
            with self.subTest(field=field):
                tampered = copy.deepcopy(receipt)
                tampered[field] = value
                with self.assertRaises(ProvenanceReleaseError):
                    verify_source_receipt(manifest, tampered)

    def test_evidence_errors_propagate_through_release(self) -> None:
        root = self.copy_fixture()
        (root / "chunk1-public.txt").write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(ProvenanceEvidenceError, "SHA"):
            adjudicate_report_release(root / "manifest.json")

    def test_cli_exposes_evidence_and_per_contract_decisions(self) -> None:
        evidence = run_cli("provenance-evidence", "--manifest", str(MANIFEST))
        self.assertEqual(evidence.returncode, 0, evidence.stderr)
        payload = json.loads(evidence.stdout)
        self.assertIsNone(payload["publication_decisions"])
        self.assertEqual(payload["provenance_status"], "ambiguous_backfill")

        release = run_cli("provenance-adjudicate", "--manifest", str(MANIFEST),
                          "--out-dir", str(self.out))
        self.assertEqual(release.returncode, 2, release.stderr)
        payload = json.loads(release.stdout)
        by_kind = decisions(payload)
        self.assertEqual(by_kind["reproducible_authorized"]["status"], "allowed")
        self.assertEqual(by_kind["origin_safe"]["status"], "blocked")
        self.assertEqual((self.out / "report-reconstructed.txt").read_bytes(),
                         (FIXTURE / "report.txt").read_bytes())
        self.assertFalse((self.out / "report-original.txt").exists())
        self.assertIn("written", payload)

    def test_cli_origin_safe_fixture_exits_zero(self) -> None:
        manifest = FIXTURE / "all_public" / "manifest.json"
        result = run_cli("provenance-adjudicate", "--manifest", str(manifest),
                         "--contract", "origin_safe", "--out-dir", str(self.out))
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["publication_decisions"][0]["publication_kind"],
                         "original_backfill_instance")


if __name__ == "__main__":
    unittest.main()
