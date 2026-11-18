"""Release-blocking fixtures: bad evidence, unsafe objectives, and replay."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from lakemirror.privacy_evidence import (
    PrivacyEvidenceError,
    inspect_privacy_table,
    release_privacy_table,
    verify_privacy_release,
)
from lakemirror.privacy_release import PrivacyReleaseError, build_privacy_release

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "privacy_table"


def copy_fixture(tmp: str) -> Path:
    target = Path(tmp) / "privacy_table"
    shutil.copytree(FIXTURES, target)
    return target


class EvidenceBlockingTests(unittest.TestCase):
    def test_bad_sha_blocks_evidence_and_release(self) -> None:
        with self.assertRaisesRegex(PrivacyEvidenceError, "SHA differs"):
            inspect_privacy_table(FIXTURES / "bad-sha-contract.json")
        with self.assertRaises(PrivacyEvidenceError):
            release_privacy_table(FIXTURES / "bad-sha-contract.json", "exact_margins")

    def test_duplicate_cell_blocks(self) -> None:
        with self.assertRaisesRegex(PrivacyEvidenceError, "duplicate or invalid cell"):
            inspect_privacy_table(FIXTURES / "duplicate-contract.json")

    def test_missing_cell_blocks(self) -> None:
        with self.assertRaisesRegex(PrivacyEvidenceError, "table grid incomplete"):
            inspect_privacy_table(FIXTURES / "missing-contract.json")

    def test_negative_count_blocks(self) -> None:
        with self.assertRaisesRegex(PrivacyEvidenceError, "duplicate or invalid cell"):
            inspect_privacy_table(FIXTURES / "negative-contract.json")

    def test_non_integer_count_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = copy_fixture(tmp)
            cells = target / "cells.csv"
            cells.write_text(cells.read_text(encoding="utf-8").replace("R1,C2,4", "R1,C2,4.5"),
                             encoding="utf-8")
            contract = json.loads((target / "contract.json").read_text(encoding="utf-8"))
            contract["cells_sha256"] = hashlib.sha256(cells.read_bytes()).hexdigest()
            (target / "contract.json").write_text(json.dumps(contract), encoding="utf-8")
            with self.assertRaisesRegex(PrivacyEvidenceError, "non-integer"):
                inspect_privacy_table(target / "contract.json")

    def test_declared_margins_must_match_the_table(self) -> None:
        with self.assertRaisesRegex(PrivacyEvidenceError, "declared margins differ"):
            inspect_privacy_table(FIXTURES / "bad-margins-contract.json")

    def test_unsynchronized_contract_terms_block(self) -> None:
        evidence = inspect_privacy_table(FIXTURES / "swapped-contract.json")
        with self.assertRaisesRegex(PrivacyReleaseError, "contract terms are unsupported"):
            build_privacy_release(evidence, "exact_margins")


class UnprotectableEvidenceTests(unittest.TestCase):
    def test_empty_table_cannot_protect_sensitive_cells(self) -> None:
        with self.assertRaisesRegex(PrivacyReleaseError, "no suppression plan"):
            release_privacy_table(FIXTURES / "unprotectable-contract.json", "exact_margins")

    def test_missing_contract_kind_blocks(self) -> None:
        with self.assertRaises(PrivacyReleaseError):
            release_privacy_table(FIXTURES / "contract.json", "partial_margins")


class ReplayBlockingTests(unittest.TestCase):
    def test_valid_release_verifies_then_old_receipt_is_refused(self) -> None:
        first = release_privacy_table(FIXTURES / "contract.json", "rounded_margins")
        self.assertTrue(verify_privacy_release(FIXTURES / "contract.json", "rounded_margins",
                                               first["public_artifact"],
                                               first["internal_receipt"])["verified"])
        with self.assertRaisesRegex(PrivacyReleaseError, "stale receipts"):
            verify_privacy_release(FIXTURES / "corrected-contract.json", "rounded_margins",
                                   first["public_artifact"], first["internal_receipt"])

    def test_corrected_release_rejects_reused_mask_from_base(self) -> None:
        base = release_privacy_table(FIXTURES / "contract.json", "rounded_margins")
        corrected = release_privacy_table(FIXTURES / "corrected-contract.json", "rounded_margins")
        self.assertNotEqual(base["internal_receipt"]["suppressed_cells"],
                            corrected["internal_receipt"]["suppressed_cells"])
        with self.assertRaises(PrivacyReleaseError):
            verify_privacy_release(FIXTURES / "corrected-contract.json", "rounded_margins",
                                   base["public_artifact"], base["internal_receipt"])

    def test_tampered_receipt_is_rejected(self) -> None:
        release = release_privacy_table(FIXTURES / "contract.json", "exact_margins")
        receipt = dict(release["internal_receipt"], suppressed_cells=["R1:C1", "R2:C3"])
        with self.assertRaisesRegex(PrivacyReleaseError, "fingerprint differs"):
            verify_privacy_release(FIXTURES / "contract.json", "exact_margins",
                                   release["public_artifact"], receipt)

    def test_malformed_artifact_contract_is_rejected(self) -> None:
        release = release_privacy_table(FIXTURES / "contract.json", "rounded_margins")
        public = dict(release["public_artifact"], contract="rounded_margins")
        with self.assertRaises(PrivacyReleaseError):
            verify_privacy_release(FIXTURES / "contract.json", "rounded_margins",
                                   public, release["internal_receipt"])


if __name__ == "__main__":
    unittest.main()
