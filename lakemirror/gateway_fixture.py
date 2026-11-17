"""Offline gateway evidence bound to the order row fixture."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .row_fixture import DAY, generate_row_fixture
from .util import LakeMirrorError, write_json


SCENARIOS = ("captured", "advance_then_dispute", "wrong_reference", "duplicate_receipt")


def generate_gateway_fixture(scenario: str, workdir: str | Path) -> dict[str, Any]:
    if scenario not in SCENARIOS:
        raise LakeMirrorError(f"unknown gateway scenario {scenario}", "GATEWAY_SCENARIO")
    root = Path(workdir).resolve()
    row = generate_row_fixture("consistent" if scenario == "captured" else "late_arrival", root)
    terms = {
        "version": 1, "merchant_id": "M100", "order_id": "O100",
        "currency": "USD", "face_amount_minor": 10000,
        "contracts": [
            {"id": "audit", "basis": "dual_replica", "advance_bps": 0,
             "loss_bearer": "merchant"},
            {"id": "liquidity", "basis": "primary_gateway", "advance_bps": 8000,
             "loss_bearer": "platform"},
        ],
    }
    capture = {
        "receipt_id": "G100", "order_id": "O100", "kind": "capture",
        "amount_minor": 10000, "currency": "USD", "source": "mock-gateway",
        "ref_receipt_id": "", "effective_at": f"{DAY}T09:05:00Z",
        "observed_at": f"{DAY}T09:15:00Z",
    }
    chargeback = {
        "receipt_id": "G101", "order_id": "O100", "kind": "chargeback",
        "amount_minor": 3000, "currency": "USD", "source": "mock-gateway",
        "ref_receipt_id": "G100", "effective_at": f"{DAY}T09:50:00Z",
        "observed_at": f"{DAY}T11:00:00Z",
    }
    receipts = [capture]
    if scenario != "captured":
        receipts.append(chargeback)
    if scenario == "wrong_reference":
        receipts[1]["ref_receipt_id"] = "G999"
    if scenario == "duplicate_receipt":
        receipts[1]["receipt_id"] = "G100"
    terms_path = root / "settlement_terms.json"
    receipts_path = root / "gateway_receipts.json"
    write_json(terms_path, terms)
    write_json(receipts_path, {"version": 1, "receipts": receipts})
    return {"scenario": scenario, "workdir": str(root), "row_contract": row["contract"],
            "terms": str(terms_path), "receipts": str(receipts_path)}
