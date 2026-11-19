"""Local merchant settlement authorization and paid-liability clearing.

The only component allowed to record an actual merchant payout.  Row contract
verdicts and gateway evidence stay read-only: a ``paid`` order, a planned net
amount, or not-yet-observed evidence can never be presented as money already
received.  Payouts, shortfalls, and reattributions persist in an append-only
JSON ledger with one payout identity and an explainable trail across runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .gateway_evidence import load_gateway_evidence
from .row_contract import evaluate_row_contract
from .util import (LakeMirrorError, atomic_write_text, fingerprint, pretty_json,
                   read_json, stable_id, utc_now, within)

LEDGER_VERSION = 1
(AWAITING_DUAL, PRIMARY_PENDING, NO_CAPTURE, GATEWAY_INVALID, ROW_UNKNOWN, ROW_BLOCKED,
 IDENTITY_MISMATCH, AMOUNT_MISMATCH, TEMPORARY_ADVANCE, FINAL_PAYOUT, REUSED,
 PLATFORM_LOSS, MERCHANT_RECOVERY) = (
    "SETTLEMENT_AWAITING_DUAL_VERIFIED", "SETTLEMENT_PRIMARY_PENDING", "SETTLEMENT_NO_VISIBLE_CAPTURE",
    "SETTLEMENT_GATEWAY_INVALID", "SETTLEMENT_ROW_UNKNOWN", "SETTLEMENT_ROW_BLOCKED",
    "SETTLEMENT_IDENTITY_MISMATCH", "SETTLEMENT_AMOUNT_MISMATCH", "SETTLEMENT_TEMPORARY_ADVANCE",
    "SETTLEMENT_FINAL_PAYOUT", "SETTLEMENT_NO_DUPLICATE_PAYOUT",
    "SETTLEMENT_PLATFORM_LOSS", "SETTLEMENT_MERCHANT_RECOVERY")


def _inside(root: Path, raw: str | Path) -> Path:
    target = Path(raw).resolve()
    if not within(root, target) or not target.is_file() or target.stat().st_size > 1024 * 1024:
        raise LakeMirrorError("settlement input must be a bounded file inside workdir", "SETTLEMENT_PATH")
    return target


class SettlementLedger:
    """Append-only payout/liability ledger with stable payout identities."""

    def __init__(self, path: Path, scope_id: str) -> None:
        self.path, self.scope_id = path, scope_id
        if path.is_file():
            data = read_json(path)
            if not isinstance(data, dict) or data.get("version") != LEDGER_VERSION \
                    or data.get("ledger_id") != scope_id:
                raise LakeMirrorError("settlement ledger identity does not match this workdir",
                                      "SETTLEMENT_LEDGER_SCOPE")
            self.data = data
        else:
            self.data = {"version": LEDGER_VERSION, "ledger_id": scope_id, "created_at": utc_now(),
                         "payouts": [], "liabilities": [], "journal": []}

    @classmethod
    def open(cls, workdir: str | Path, raw: str | Path | None = None) -> "SettlementLedger":
        root = Path(workdir).resolve()
        path = Path(raw).resolve() if raw else (root / "settlement_ledger.json").resolve()
        if not within(root, path):
            raise LakeMirrorError("settlement ledger must stay inside workdir", "SETTLEMENT_LEDGER_PATH")
        return cls(path, stable_id("sled", str(root)))

    def save(self) -> None:
        atomic_write_text(self.path, pretty_json(self.data))
    def journal(self, event: str, *, contract_id: str | None = None, code: str = "",
                detail: dict[str, Any] | None = None, as_of: str = "") -> None:
        self.data["journal"].append({"seq": len(self.data["journal"]) + 1, "at": utc_now(),
                                     "as_of": as_of, "event": event, "contract_id": contract_id,
                                     "code": code, "detail": detail or {}})
    def payout_id(self, contract_id: str, terms: dict[str, Any]) -> str:
        return stable_id("payout", self.scope_id, terms["order_id"], terms["merchant_id"],
                         terms["currency"], contract_id)

    def payout(self, payout_id: str) -> dict[str, Any] | None:
        return next((x for x in self.data["payouts"] if x["payout_id"] == payout_id), None)

    def add_payout(self, payout_id: str, contract: dict[str, Any], terms: dict[str, Any],
                   evidence: dict[str, Any], amount: int, kind: str, as_of: str) -> dict[str, Any]:
        existing = self.payout(payout_id)
        if existing is not None:
            return existing
        record = {
            "payout_id": payout_id, "contract_id": contract["id"], "order_id": terms["order_id"],
            "merchant_id": terms["merchant_id"], "currency": terms["currency"],
            "receipt_ids": sorted(item["receipt_id"] for item in evidence["visible"]),
            "amount_minor": amount, "kind": kind, "provisional": kind == "advance",
            "status": "paid", "terms_fingerprint": fingerprint(terms),
            "receipt_fingerprint": evidence["receipt_fingerprint"], "authorized_as_of": as_of,
            "created_at": utc_now(),
            "evidence": {
                "advance_cap_minor": (terms["face_amount_minor"] * contract["advance_bps"]) // 10000,
                "visible_net_minor": _visible_net(evidence),
                "visible_receipt_ids": [item["receipt_id"] for item in evidence["visible"]]},
        }
        self.data["payouts"].append(record)
        return record

    def record_liability(self, contract: dict[str, Any], payout: dict[str, Any], terms: dict[str, Any],
                         evidence: dict[str, Any], net: int, as_of: str) -> dict[str, Any]:
        shortfall, bearer = payout["amount_minor"] - net, contract["loss_bearer"]
        liability_id = stable_id("liab", self.scope_id, terms["order_id"], contract["id"],
                                 payout["payout_id"], shortfall, bearer)
        existing = next((x for x in self.data["liabilities"] if x["liability_id"] == liability_id), None)
        if existing is not None:
            return existing
        prior = next((item for item in self.data["liabilities"]
                      if item["contract_id"] == contract["id"] and item["payout_id"] == payout["payout_id"]
                      and item["status"] != "reattributed"), None)
        if prior is not None and prior["obligation_party"] != bearer:
            prior["status"], prior["superseded_at"] = "reattributed", utc_now()
            self.journal("liability_reattributed", contract_id=contract["id"],
                         code=MERCHANT_RECOVERY if bearer == "merchant" else PLATFORM_LOSS,
                         detail={"from": prior["liability_id"], "to": liability_id}, as_of=as_of)
        record = {
            "liability_id": liability_id, "contract_id": contract["id"],
            "payout_id": payout["payout_id"], "order_id": terms["order_id"],
            "merchant_id": terms["merchant_id"], "currency": terms["currency"],
            "paid_minor": payout["amount_minor"], "net_minor": net, "shortfall_minor": shortfall,
            "loss_bearer": bearer, "obligation_party": bearer,
            "recovers_from_merchant_minor": shortfall if bearer == "merchant" else 0,
            "platform_loss_minor": shortfall if bearer == "platform" else 0,
            "status": "merchant_recovery" if bearer == "merchant" else "platform_loss",
            "replaces": prior["liability_id"] if prior and prior["status"] == "reattributed" else None,
            "receipt_ids": sorted(item["receipt_id"] for item in evidence["visible"]),
            "terms_fingerprint": fingerprint(terms), "as_of": as_of, "created_at": utc_now()}
        self.data["liabilities"].append(record)
        return record


def _visible_net(evidence: dict[str, Any]) -> int:
    return sum(x["amount_minor"] if x["kind"] == "capture" else -x["amount_minor"] for x in evidence["visible"])
def _order(side: dict[str, Any], order_id: str) -> dict[str, Any] | None:
    return next((x for x in side.get("orders", []) if x["order_id"] == order_id), None)
def _decision(contract: dict[str, Any], status: str, action: str, code: str,
              payout_id: str | None = None, liability_id: str | None = None,
              detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"contract_id": contract["id"], "basis": contract["basis"], "status": status,
            "action": action, "code": code, "payout_id": payout_id, "liability_id": liability_id,
            "detail": detail or {}}


def _anomaly(code: str, ledger: SettlementLedger, contract: dict[str, Any], terms: dict[str, Any],
             as_of: str, detail: dict[str, Any]) -> dict[str, Any]:
    payout = ledger.payout(ledger.payout_id(contract["id"], terms))
    if payout is None:
        ledger.journal("decision_blocked", contract_id=contract["id"], code=code, detail=detail, as_of=as_of)
        return _decision(contract, "blocked", "none", code, detail=detail)
    if payout["status"] == "paid":
        payout["status"], payout["under_review_since"] = "under_review", utc_now()
    ledger.journal("payout_under_review", contract_id=contract["id"], code=code,
                   detail={"payout_id": payout["payout_id"], "amount_minor": payout["amount_minor"]},
                   as_of=as_of)
    return _decision(contract, "under_review", "mark_under_review", code,
                     payout_id=payout["payout_id"], detail=detail)


def _authorize(ledger: SettlementLedger, contract: dict[str, Any], terms: dict[str, Any],
               evidence: dict[str, Any], amount: int, kind: str, code: str, as_of: str) -> dict[str, Any]:
    payout_id = ledger.payout_id(contract["id"], terms)
    existing = ledger.payout(payout_id)
    if existing is not None:
        ledger.journal("payout_reused", contract_id=contract["id"], code=REUSED,
                       detail={"payout_id": payout_id, "amount_minor": existing["amount_minor"]}, as_of=as_of)
        return _decision(contract, "settled", "reuse_payout", REUSED, payout_id=payout_id,
                         detail={"paid_minor": existing["amount_minor"]})
    payout = ledger.add_payout(payout_id, contract, terms, evidence, amount, kind, as_of)
    ledger.journal("payout_authorized", contract_id=contract["id"], code=code,
                   detail={"payout_id": payout_id, "amount_minor": amount, "kind": kind}, as_of=as_of)
    return _decision(contract, "paid", "payout", code, payout_id=payout_id,
                     detail={"amount_minor": amount, "kind": kind, "provisional": payout["provisional"]})


def _clear(ledger: SettlementLedger, contract: dict[str, Any], terms: dict[str, Any],
           evidence: dict[str, Any], as_of: str) -> dict[str, Any]:
    payout_id = ledger.payout_id(contract["id"], terms)
    payout = ledger.payout(payout_id)
    if payout is None:
        return _authorize(ledger, contract, terms, evidence, _visible_net(evidence),
                          "settlement", FINAL_PAYOUT, as_of)
    net = _visible_net(evidence)
    liability = (ledger.record_liability(contract, payout, terms, evidence, net, as_of)
                 if payout["amount_minor"] > net else None)
    if payout["provisional"]:
        payout["provisional"], payout["status"], payout["cleared_as_of"] = False, "cleared", as_of
        ledger.journal("advance_cleared", contract_id=contract["id"], code=REUSED,
                       detail={"payout_id": payout_id, "net_minor": net}, as_of=as_of)
    if liability is None:
        ledger.journal("payout_reused", contract_id=contract["id"], code=REUSED,
                       detail={"payout_id": payout_id, "residual_minor": max(0, net - payout["amount_minor"])},
                       as_of=as_of)
        return _decision(contract, "settled", "reuse_payout", REUSED, payout_id=payout_id,
                         detail={"paid_minor": payout["amount_minor"], "net_minor": net,
                                 "residual_minor": max(0, net - payout["amount_minor"])})
    code = PLATFORM_LOSS if liability["obligation_party"] == "platform" else MERCHANT_RECOVERY
    ledger.journal("liability_recorded", contract_id=contract["id"], code=code,
                   detail={"liability_id": liability["liability_id"],
                           "shortfall_minor": liability["shortfall_minor"]}, as_of=as_of)
    return _decision(contract, "settled", "reuse_payout", code, payout_id=payout_id,
                     liability_id=liability["liability_id"],
                     detail={"paid_minor": payout["amount_minor"], "net_minor": net,
                             "shortfall_minor": liability["shortfall_minor"],
                             "obligation_party": liability["obligation_party"]})


def _side_anomaly(side: dict[str, Any]) -> str | None:
    if side.get("evidence_error") or side["status"] == "unknown":
        return ROW_UNKNOWN
    return ROW_BLOCKED if side["status"] == "inconsistent" else None
def _held(ledger: SettlementLedger, contract: dict[str, Any], code: str,
          as_of: str, detail: dict[str, Any]) -> dict[str, Any]:
    ledger.journal("decision_held", contract_id=contract["id"], code=code, detail=detail, as_of=as_of)
    return _decision(contract, "held", "none", code, detail=detail)


def _evaluate_contract(ledger: SettlementLedger, contract: dict[str, Any], terms: dict[str, Any],
                       row: dict[str, Any], evidence: dict[str, Any] | None,
                       gateway_error: LakeMirrorError | None, as_of: str) -> dict[str, Any]:
    if gateway_error is not None:
        return _anomaly(GATEWAY_INVALID, ledger, contract, terms, as_of,
                        {"gateway_code": gateway_error.code, "message": str(gateway_error)})
    primary, replica = row["sides"]["primary"], row["sides"]["replica"]
    needed = ("primary", "replica") if contract["basis"] == "dual_replica" else ("primary",)
    for side in needed:  # physical unknown / business blocked outrank per-order identity
        if code := _side_anomaly(row["sides"][side]):
            order = _order(row["sides"][side], terms["order_id"])
            reason = order["reason"] if order else row["sides"][side].get("evidence_error", side)
            return _anomaly(code, ledger, contract, terms, as_of, {"side": side, "reason": reason})
    target_primary = _order(primary, terms["order_id"])
    target_replica = _order(replica, terms["order_id"]) if contract["basis"] == "dual_replica" else target_primary
    if target_primary is None or target_replica is None:
        return _anomaly(IDENTITY_MISMATCH, ledger, contract, terms, as_of,
                        {"order_id": terms["order_id"], "primary_found": target_primary is not None,
                         "replica_found": target_replica is not None})
    visible_capture = sum(x["amount_minor"] for x in evidence["visible"] if x["kind"] == "capture")
    if visible_capture > terms["face_amount_minor"]:
        return _anomaly(AMOUNT_MISMATCH, ledger, contract, terms, as_of,
                        {"face_amount_minor": terms["face_amount_minor"], "capture_minor": visible_capture})
    if contract["basis"] == "dual_replica":
        if target_primary["status"] != "verified" or target_replica["status"] != "verified":
            return _held(ledger, contract, AWAITING_DUAL, as_of,
                         {"primary": target_primary["status"], "replica": target_replica["status"],
                          "replica_reason": target_replica["reason"]})
        return _clear(ledger, contract, terms, evidence, as_of)
    if target_primary["status"] != "verified":
        return _held(ledger, contract, PRIMARY_PENDING, as_of, {"primary": target_primary["status"]})
    if not [x for x in evidence["visible"] if x["kind"] == "capture"]:
        return _held(ledger, contract, NO_CAPTURE, as_of, {})
    if ledger.payout(ledger.payout_id(contract["id"], terms)) is not None:
        return _clear(ledger, contract, terms, evidence, as_of)
    cap = (terms["face_amount_minor"] * contract["advance_bps"]) // 10000
    return _authorize(ledger, contract, terms, evidence, min(cap, _visible_net(evidence)),
                      "advance", TEMPORARY_ADVANCE, as_of)


def run_settlement(workdir: str | Path, receipts_path: str | Path, terms_path: str | Path,
                   contract_path: str | Path, as_of: str, *, ledger_path: str | Path | None = None,
                   contract_id: str | None = None) -> dict[str, Any]:
    """Authorize at most one payout per contract and clear paid liabilities."""
    root = Path(workdir).resolve()
    terms = read_json(_inside(root, terms_path))
    if not isinstance(terms, dict) or not isinstance(terms.get("contracts"), list):
        raise LakeMirrorError("settlement terms are malformed", "SETTLEMENT_TERMS_INVALID")
    _inside(root, receipts_path)
    _inside(root, contract_path)
    contracts = [item for item in terms["contracts"] if contract_id is None or item["id"] == contract_id]
    if contract_id is not None and not contracts:
        raise LakeMirrorError(f"unknown settlement contract {contract_id}", "SETTLEMENT_CONTRACT_UNKNOWN")
    ledger = SettlementLedger.open(root, ledger_path)
    row = evaluate_row_contract(root, contract_path, as_of)
    gateway_error: LakeMirrorError | None = None
    evidence: dict[str, Any] | None = None
    try:
        evidence = load_gateway_evidence(root, receipts_path, terms_path, as_of=as_of)
    except LakeMirrorError as exc:
        gateway_error = exc
    ledger.journal("settlement_run", as_of=as_of,
                   detail={"as_of": as_of, "row_consumer_status": row["consumer_status"],
                           "gateway_valid": gateway_error is None,
                           "gateway_code": gateway_error.code if gateway_error else ""})
    decisions = [_evaluate_contract(ledger, item, terms, row, evidence, gateway_error, as_of)
                 for item in contracts]
    ledger.save()
    payouts = [item for item in ledger.data["payouts"]
               if item["order_id"] == terms["order_id"] and item["merchant_id"] == terms["merchant_id"]]
    liabilities = [item for item in ledger.data["liabilities"]
                   if item["order_id"] == terms["order_id"] and item["status"] != "reattributed"]
    return {
        "as_of": as_of, "workdir": str(root), "ledger": str(ledger.path),
        "terms_fingerprint": fingerprint(terms),
        "receipt_fingerprint": evidence["receipt_fingerprint"] if evidence else None,
        "row_consumer_status": row["consumer_status"], "gateway_valid": gateway_error is None,
        "decisions": decisions,
        "totals": {
            "paid_minor": sum(item["amount_minor"] for item in payouts),
            "outstanding_advance_minor": sum(item["amount_minor"] for item in payouts if item["provisional"]),
            "platform_loss_minor": sum(item["platform_loss_minor"] for item in liabilities),
            "merchant_recovery_minor": sum(item["recovers_from_merchant_minor"] for item in liabilities),
            "under_review_payouts": sum(1 for item in payouts if item["status"] == "under_review")}}
