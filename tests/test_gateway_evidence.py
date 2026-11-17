"""Gateway intake is real and read-only, while payout remains unimplemented."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lakemirror.gateway_evidence import load_gateway_evidence
from lakemirror.gateway_fixture import generate_gateway_fixture
from lakemirror.row_contract import evaluate_row_contract
from lakemirror.util import LakeMirrorError, sha256_file


def _facts(root: Path) -> dict[str, str]:
    return {str(item.relative_to(root)): sha256_file(item)
            for item in root.rglob("*") if item.is_file()}


class GatewayEvidenceTests(unittest.TestCase):
    def test_as_of_receipt_and_row_evidence_diverge_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = generate_gateway_fixture("advance_then_dispute", root)
            before = _facts(root)
            early = load_gateway_evidence(root, fixture["receipts"], fixture["terms"],
                                          as_of="2024-03-01T10:00:00Z")
            row_early = evaluate_row_contract(root, fixture["row_contract"])
            later = load_gateway_evidence(root, fixture["receipts"], fixture["terms"],
                                          as_of="2024-03-01T13:00:00Z")
            row_later = evaluate_row_contract(root, fixture["row_contract"],
                                              "2024-03-01T13:00:00Z")
            self.assertEqual([x["receipt_id"] for x in early["visible"]], ["G100"])
            self.assertEqual(early["future_receipt_ids"], ["G101"])
            self.assertEqual(row_early["consumer_status"], "pending")
            self.assertEqual([x["receipt_id"] for x in later["visible"]], ["G100", "G101"])
            self.assertEqual(row_later["consumer_status"], "ready")
            self.assertEqual(before, _facts(root))

    def test_bad_parent_and_duplicate_receipts_are_rejected(self) -> None:
        for scenario, code in (("wrong_reference", "GATEWAY_REFERENCE"),
                               ("duplicate_receipt", "GATEWAY_DUPLICATE")):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temp:
                fixture = generate_gateway_fixture(scenario, temp)
                with self.assertRaises(LakeMirrorError) as caught:
                    load_gateway_evidence(temp, fixture["receipts"], fixture["terms"],
                                          as_of="2024-03-01T13:00:00Z")
                self.assertEqual(caught.exception.code, code)

    def test_external_path_and_wrong_owner_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "work"
            fixture = generate_gateway_fixture("captured", root)
            with self.assertRaisesRegex(LakeMirrorError, "inside workdir"):
                load_gateway_evidence(root, Path(temp) / "outside.json", fixture["terms"],
                                      as_of="2024-03-01T10:00:00Z")
            payload = json.loads(Path(fixture["receipts"]).read_text(encoding="utf-8"))
            payload["receipts"][0]["order_id"] = "O999"
            Path(fixture["receipts"]).write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(LakeMirrorError) as caught:
                load_gateway_evidence(root, fixture["receipts"], fixture["terms"],
                                      as_of="2024-03-01T10:00:00Z")
            self.assertEqual(caught.exception.code, "GATEWAY_OWNER")

    def test_cli_fixture_and_receipt_intake(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = subprocess.run(
                [sys.executable, "-m", "lakemirror", "gateway-fixture", "--scenario",
                 "advance_then_dispute", "--workdir", temp],
                check=True, capture_output=True, text=True,
            )
            paths = json.loads(fixture.stdout)
            evidence = subprocess.run(
                [sys.executable, "-m", "lakemirror", "gateway-evidence", "--workdir", temp,
                 "--receipts", paths["receipts"], "--terms", paths["terms"],
                 "--as-of", "2024-03-01T10:00:00Z"],
                check=True, capture_output=True, text=True,
            )
            self.assertEqual(len(json.loads(evidence.stdout)["visible"]), 1)


if __name__ == "__main__":
    unittest.main()
