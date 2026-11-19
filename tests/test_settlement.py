"""Settlement authorization, clearing, bearer switch, and idempotency tests.

Both authorized routes run on the same O100 order and gateway evidence:
the audit contract pays the merchant 7000 once dual replicas verify, while the
liquidity contract advances 8000 provisionally and later books the 1000
shortfall to the configured loss bearer without paying twice.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lakemirror.settlement import (
    AWAITING_DUAL, FINAL_PAYOUT, MERCHANT_RECOVERY, PLATFORM_LOSS,
    REUSED, TEMPORARY_ADVANCE, run_settlement,
)

from tests.settlement_fixtures import (
    EARLY, LATE, corrupt_primary_events, ledger_doc, positive_workspace, set_loss_bearer,
)


def _run(fixture: dict[str, str], as_of: str, **kwargs):
    return run_settlement(fixture["workdir"], fixture["receipts"], fixture["terms"],
                          fixture["row_contract"], as_of, **kwargs)


def _by_id(result: dict, contract_id: str) -> dict:
    return next(item for item in result["decisions"] if item["contract_id"] == contract_id)


class AdvanceThenDisputeSequenceTests(unittest.TestCase):
    def test_audit_holds_at_ten_and_pays_net_at_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = positive_workspace(tmp)
            early = _run(fixture, EARLY)
            audit = _by_id(early, "audit")
            self.assertEqual(audit["status"], "held")
            self.assertEqual(audit["code"], AWAITING_DUAL)
            self.assertEqual(audit["payout_id"], None)
            self.assertEqual(early["totals"]["paid_minor"], 8000)  # only liquidity paid
            later = _run(fixture, LATE)
            audit = _by_id(later, "audit")
            self.assertEqual(audit["status"], "paid")
            self.assertEqual(audit["code"], FINAL_PAYOUT)
            self.assertEqual(audit["detail"]["amount_minor"], 7000)
            self.assertFalse(audit["detail"]["provisional"])
            self.assertEqual(later["row_consumer_status"], "ready")

    def test_liquidity_advances_eight_then_clears_without_double_pay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = positive_workspace(tmp)
            early = _run(fixture, EARLY, contract_id="liquidity")
            decision = _by_id(early, "liquidity")
            self.assertEqual(decision["status"], "paid")
            self.assertEqual(decision["code"], TEMPORARY_ADVANCE)
            self.assertEqual(decision["detail"]["amount_minor"], 8000)
            self.assertTrue(decision["detail"]["provisional"])
            self.assertEqual(early["totals"]["outstanding_advance_minor"], 8000)
            payout_id = decision["payout_id"]
            later = _run(fixture, LATE, contract_id="liquidity")
            decision = _by_id(later, "liquidity")
            self.assertEqual(decision["status"], "settled")
            self.assertEqual(decision["payout_id"], payout_id)  # same identity, no second payout
            self.assertEqual(decision["code"], PLATFORM_LOSS)
            self.assertEqual(decision["detail"]["shortfall_minor"], 1000)
            self.assertEqual(decision["detail"]["obligation_party"], "platform")
            self.assertEqual(later["totals"]["paid_minor"], 8000)
            self.assertEqual(later["totals"]["platform_loss_minor"], 1000)
            self.assertEqual(later["totals"]["outstanding_advance_minor"], 0)

    def test_both_routes_share_order_merchant_currency_and_receipt_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = positive_workspace(tmp)
            _run(fixture, EARLY)
            _run(fixture, LATE)
            doc = ledger_doc(fixture)
            self.assertEqual(len(doc["payouts"]), 2)
            for payout in doc["payouts"]:
                self.assertEqual((payout["order_id"], payout["merchant_id"], payout["currency"]),
                                 ("O100", "M100", "USD"))
                self.assertTrue(payout["payout_id"].startswith("payout-"))
            by_contract = {p["contract_id"]: p for p in doc["payouts"]}
            self.assertEqual(by_contract["audit"]["amount_minor"], 7000)
            self.assertEqual(by_contract["liquidity"]["amount_minor"], 8000)
            self.assertIn("G100", by_contract["liquidity"]["receipt_ids"])
            self.assertEqual(by_contract["audit"]["receipt_ids"], ["G100", "G101"])

    def test_chargeback_late_arrival_changes_clearing_not_advance_fact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = positive_workspace(tmp)
            _run(fixture, EARLY, contract_id="liquidity")
            doc = ledger_doc(fixture)
            advance_id = doc["payouts"][0]["payout_id"]
            self.assertEqual(doc["payouts"][0]["amount_minor"], 8000)
            _run(fixture, LATE, contract_id="liquidity")
            doc = ledger_doc(fixture)
            self.assertEqual(len(doc["payouts"]), 1)  # the new 7000 net is never re-paid
            self.assertEqual(doc["payouts"][0]["payout_id"], advance_id)
            self.assertEqual(doc["payouts"][0]["status"], "cleared")
            self.assertEqual(len(doc["liabilities"]), 1)
            self.assertEqual(doc["liabilities"][0]["shortfall_minor"], 1000)


class IdempotencyTests(unittest.TestCase):
    def test_repeating_same_contract_and_time_keeps_single_payout_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = positive_workspace(tmp)
            first = _run(fixture, EARLY)
            second = _run(fixture, EARLY)
            third = _run(fixture, EARLY)
            for result in (first, second, third):
                self.assertEqual(_by_id(result, "liquidity")["code"],
                                 TEMPORARY_ADVANCE if result is first else REUSED)
            doc = ledger_doc(fixture)
            self.assertEqual(len(doc["payouts"]), 1)
            self.assertEqual(doc["payouts"][0]["amount_minor"], 8000)

    def test_cross_time_reruns_append_journal_without_mutating_payout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = positive_workspace(tmp)
            _run(fixture, EARLY, contract_id="liquidity")
            _run(fixture, LATE, contract_id="liquidity")
            _run(fixture, LATE, contract_id="liquidity")
            doc = ledger_doc(fixture)
            self.assertEqual(len(doc["payouts"]), 1)
            self.assertEqual(doc["payouts"][0]["amount_minor"], 8000)
            self.assertEqual(sum(1 for e in doc["journal"] if e["event"] == "payout_authorized"), 1)
            self.assertTrue(any(e["event"] == "liability_recorded" for e in doc["journal"]))

    def test_audit_second_run_after_payout_reuses_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = positive_workspace(tmp)
            first = _run(fixture, LATE, contract_id="audit")
            payout_id = _by_id(first, "audit")["payout_id"]
            second = _run(fixture, LATE, contract_id="audit")
            self.assertEqual(_by_id(second, "audit")["code"], REUSED)
            self.assertEqual(_by_id(second, "audit")["payout_id"], payout_id)
            self.assertEqual(len(ledger_doc(fixture)["payouts"]), 1)


class LossBearerTests(unittest.TestCase):
    def test_platform_then_merchant_switch_reattributes_without_rewriting_payout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = positive_workspace(tmp)
            _run(fixture, EARLY, contract_id="liquidity")
            platform = _run(fixture, LATE, contract_id="liquidity")
            self.assertEqual(_by_id(platform, "liquidity")["code"], PLATFORM_LOSS)
            set_loss_bearer(fixture, "liquidity", "merchant")
            merchant = _run(fixture, LATE, contract_id="liquidity")
            decision = _by_id(merchant, "liquidity")
            self.assertEqual(decision["code"], MERCHANT_RECOVERY)
            self.assertEqual(decision["detail"]["obligation_party"], "merchant")
            self.assertEqual(merchant["totals"]["merchant_recovery_minor"], 1000)
            self.assertEqual(merchant["totals"]["platform_loss_minor"], 0)
            doc = ledger_doc(fixture)
            self.assertEqual(len(doc["payouts"]), 1)  # payout fact never rewritten
            self.assertEqual(doc["payouts"][0]["amount_minor"], 8000)
            statuses = sorted(item["status"] for item in doc["liabilities"])
            self.assertEqual(statuses, ["merchant_recovery", "reattributed"])
            self.assertTrue(any(e["event"] == "liability_reattributed" for e in doc["journal"]))

    def test_merchant_bearer_from_the_start_books_recovery_obligation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = positive_workspace(tmp)
            set_loss_bearer(fixture, "liquidity", "merchant")
            _run(fixture, EARLY, contract_id="liquidity")
            result = _run(fixture, LATE, contract_id="liquidity")
            decision = _by_id(result, "liquidity")
            self.assertEqual(decision["code"], MERCHANT_RECOVERY)
            self.assertEqual(result["totals"]["merchant_recovery_minor"], 1000)
            doc = ledger_doc(fixture)
            self.assertEqual(doc["liabilities"][0]["recovers_from_merchant_minor"], 1000)
            self.assertEqual(doc["liabilities"][0]["platform_loss_minor"], 0)

    def test_pending_is_never_recorded_as_verified_payout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = positive_workspace(tmp)
            result = _run(fixture, EARLY)
            self.assertEqual(_by_id(result, "audit")["status"], "held")
            doc = ledger_doc(fixture)
            self.assertEqual([p for p in doc["payouts"] if p["contract_id"] == "audit"], [])
            # the held audit contract must never appear as a verified payout
            self.assertEqual(_by_id(result, "audit")["detail"]["replica"], "pending")
            self.assertNotIn("amount_minor", _by_id(result, "audit")["detail"])


class EvidenceCorruptionAfterPayoutTests(unittest.TestCase):
    def test_primary_corruption_marks_advance_under_review_but_keeps_fact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = positive_workspace(tmp)
            _run(fixture, EARLY, contract_id="liquidity")
            corrupt_primary_events(fixture)
            result = _run(fixture, LATE, contract_id="liquidity")
            decision = _by_id(result, "liquidity")
            self.assertEqual(decision["status"], "under_review")
            self.assertEqual(result["totals"]["under_review_payouts"], 1)
            doc = ledger_doc(fixture)
            self.assertEqual(len(doc["payouts"]), 1)
            self.assertEqual(doc["payouts"][0]["amount_minor"], 8000)
            self.assertEqual(doc["payouts"][0]["status"], "under_review")


if __name__ == "__main__":
    unittest.main()
