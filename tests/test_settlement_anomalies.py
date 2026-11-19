"""Negative evidence, ledger safety, and CLI acceptance for settlement.

Invalid gateway evidence, unknown physical row evidence, missing capture, and
ledger scope/path guards must never authorize a new payout.  A payout already
on the books is preserved and flagged, never deleted to manufacture zero risk.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lakemirror.settlement import (
    GATEWAY_INVALID, NO_CAPTURE, ROW_BLOCKED, ROW_UNKNOWN, run_settlement,
)
from lakemirror.util import LakeMirrorError, write_json

from tests.settlement_fixtures import (
    EARLY, LATE, corrupt_replica_events, ledger_doc, negative_gateway_workspace,
    positive_workspace,
)

ROOT = Path(__file__).resolve().parents[1]


def _run(fixture: dict[str, str], as_of: str, **kwargs):
    return run_settlement(fixture["workdir"], fixture["receipts"], fixture["terms"],
                          fixture["row_contract"], as_of, **kwargs)


def _defer_capture_past_cutoff(fixture: dict[str, str]) -> None:
    """Keep the envelope valid but make the only capture unobserved at as_of."""
    path = Path(fixture["receipts"])
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["receipts"] = [r for r in doc["receipts"] if r["kind"] == "capture"]
    doc["receipts"][0]["observed_at"] = "2024-03-01T14:00:00Z"
    write_json(path, doc)


def _cli(fixture: dict[str, str], as_of: str, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "lakemirror", "settle", "--workdir", fixture["workdir"],
         "--receipts", fixture["receipts"], "--terms", fixture["terms"],
         "--contract", fixture["row_contract"], "--as-of", as_of, *extra],
        cwd=str(ROOT), text=True, capture_output=True, timeout=120)


class GatewayEvidenceAnomalyTests(unittest.TestCase):
    def test_duplicate_and_wrong_reference_block_new_payouts(self) -> None:
        for scenario in ("duplicate_receipt", "wrong_reference"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                fixture = negative_gateway_workspace(tmp, scenario)
                result = _run(fixture, LATE)
                self.assertFalse(result["gateway_valid"])
                for decision in result["decisions"]:
                    self.assertEqual(decision["status"], "blocked")
                    self.assertEqual(decision["code"], GATEWAY_INVALID)
                self.assertEqual(result["totals"]["paid_minor"], 0)
                self.assertEqual(ledger_doc(fixture)["payouts"], [])

    def test_gateway_failure_after_real_payout_keeps_fact_under_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = positive_workspace(tmp)
            _run(fixture, EARLY, contract_id="liquidity")
            path = Path(fixture["receipts"])
            doc = json.loads(path.read_text(encoding="utf-8"))
            # Introduce a duplicate receipt id to invalidate the gateway intake.
            doc["receipts"].append(dict(doc["receipts"][0]))
            write_json(path, doc)
            result = _run(fixture, LATE, contract_id="liquidity")
            decision = result["decisions"][0]
            self.assertEqual(decision["status"], "under_review")
            self.assertEqual(decision["code"], GATEWAY_INVALID)
            stored = ledger_doc(fixture)
            self.assertEqual(len(stored["payouts"]), 1)
            self.assertEqual(stored["payouts"][0]["status"], "under_review")
            self.assertEqual(stored["payouts"][0]["amount_minor"], 8000)


class RowEvidenceAnomalyTests(unittest.TestCase):
    def test_replica_corruption_blocks_audit_without_paying(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = positive_workspace(tmp)
            held = _run(fixture, EARLY, contract_id="audit")
            self.assertEqual(held["decisions"][0]["status"], "held")
            corrupt_replica_events(fixture)
            result = _run(fixture, LATE, contract_id="audit")
            self.assertEqual(result["decisions"][0]["code"], ROW_UNKNOWN)
            self.assertEqual(result["decisions"][0]["status"], "blocked")
            self.assertEqual(ledger_doc(fixture)["payouts"], [])

    def test_business_contradiction_blocks_dual_replica_audit_route(self) -> None:
        from lakemirror.gateway_fixture import generate_gateway_fixture
        from lakemirror.row_fixture import generate_row_fixture
        with tempfile.TemporaryDirectory() as tmp:
            # state_conflict only contradicts the replica; the primary-gateway
            # liquidity route stays valid, while the dual-replica audit must block.
            root = Path(tmp).resolve() / "conflict"
            row = generate_row_fixture("state_conflict", root)
            donor = generate_gateway_fixture("captured", Path(tmp).resolve() / "donor")
            for name in ("gateway_receipts.json", "settlement_terms.json"):
                (root / name).write_text((Path(donor["workdir"]) / name).read_text(encoding="utf-8"),
                                         encoding="utf-8")
            result = run_settlement(root, root / "gateway_receipts.json",
                                    root / "settlement_terms.json", row["contract"], LATE,
                                    contract_id="audit")
            decision = result["decisions"][0]
            self.assertEqual(decision["code"], ROW_BLOCKED)
            self.assertEqual(decision["status"], "blocked")
            self.assertEqual(ledger_doc({"workdir": str(root)})["payouts"], [])


class MissingCaptureAndLedgerSafetyTests(unittest.TestCase):
    def test_liquidity_without_visible_capture_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = positive_workspace(tmp)
            _defer_capture_past_cutoff(fixture)
            result = _run(fixture, LATE, contract_id="liquidity")
            self.assertEqual(result["decisions"][0]["status"], "held")
            self.assertEqual(result["decisions"][0]["code"], NO_CAPTURE)
            self.assertTrue(result["gateway_valid"])
            self.assertEqual(ledger_doc(fixture)["payouts"], [])

    def test_unknown_contract_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = positive_workspace(tmp)
            with self.assertRaises(LakeMirrorError) as caught:
                _run(fixture, EARLY, contract_id="nope")
            self.assertEqual(caught.exception.code, "SETTLEMENT_CONTRACT_UNKNOWN")

    def test_ledger_path_must_stay_inside_workdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = positive_workspace(tmp)
            outside = Path(tmp).resolve() / "outside-ledger.json"
            with self.assertRaises(LakeMirrorError) as caught:
                _run(fixture, EARLY, ledger_path=outside)
            self.assertEqual(caught.exception.code, "SETTLEMENT_LEDGER_PATH")

    def test_external_input_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = positive_workspace(tmp)
            outside = Path(tmp).resolve() / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            with self.assertRaises(LakeMirrorError) as caught:
                run_settlement(fixture["workdir"], outside, fixture["terms"],
                               fixture["row_contract"], EARLY)
            self.assertEqual(caught.exception.code, "SETTLEMENT_PATH")


class SettlementCliTests(unittest.TestCase):
    def test_cli_full_sequence_and_idempotent_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = positive_workspace(tmp)
            early = json.loads(_cli(fixture, EARLY).stdout)
            self.assertEqual(next(d for d in early["decisions"] if d["contract_id"] == "audit")["status"], "held")
            self.assertEqual(early["totals"]["paid_minor"], 8000)
            later = json.loads(_cli(fixture, LATE).stdout)
            codes = {d["contract_id"]: d["code"] for d in later["decisions"]}
            self.assertEqual(codes["audit"], "SETTLEMENT_FINAL_PAYOUT")
            self.assertEqual(codes["liquidity"], "SETTLEMENT_PLATFORM_LOSS")
            self.assertEqual(later["totals"]["platform_loss_minor"], 1000)
            rerun = json.loads(_cli(fixture, LATE).stdout)
            self.assertEqual(rerun["totals"]["paid_minor"], later["totals"]["paid_minor"])
            self.assertTrue(Path(fixture["workdir"], "settlement_ledger.json").is_file())

    def test_cli_contract_id_filter_runs_single_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = positive_workspace(tmp)
            result = json.loads(_cli(fixture, EARLY, "--contract-id", "liquidity").stdout)
            self.assertEqual(len(result["decisions"]), 1)
            self.assertEqual(result["decisions"][0]["contract_id"], "liquidity")


if __name__ == "__main__":
    unittest.main()
