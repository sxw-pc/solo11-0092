"""Bounded intake for mock gateway receipts and merchant settlement terms.

This module validates source evidence; it does not authorize any payout.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .util import LakeMirrorError, fingerprint, parse_timestamp, read_json, within


ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
RECEIPT_KEYS = {"receipt_id", "order_id", "kind", "amount_minor", "currency",
                "source", "ref_receipt_id", "effective_at", "observed_at"}


def _load_inside(root: Path, path: str | Path) -> Any:
    target = Path(path).resolve()
    if not within(root, target) or not target.is_file() or target.stat().st_size > 1024 * 1024:
        raise LakeMirrorError("evidence must be a bounded file inside workdir", "GATEWAY_PATH")
    return read_json(target)


def _id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise LakeMirrorError(f"{field} must be a bounded identifier", "GATEWAY_INVALID")
    return value


def _time(value: Any, field: str):
    if not isinstance(value, str) or parse_timestamp(value) is None:
        raise LakeMirrorError(f"{field} must be a valid timestamp", "GATEWAY_INVALID")
    return parse_timestamp(value)


def _amount(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > 10**12:
        raise LakeMirrorError(f"{field} must be a bounded positive integer", "GATEWAY_INVALID")
    return value


def load_gateway_evidence(
    workdir: str | Path, receipts_path: str | Path, terms_path: str | Path,
    *, as_of: str,
) -> dict[str, Any]:
    """Return validated visible evidence, including unresolved future receipts."""
    root = Path(workdir).resolve()
    cutoff = _time(as_of, "as_of")
    terms = _load_inside(root, terms_path)
    if not isinstance(terms, dict) or set(terms) != {
        "version", "merchant_id", "order_id", "currency", "face_amount_minor", "contracts"
    } or terms["version"] != 1:
        raise LakeMirrorError("settlement terms are malformed", "GATEWAY_INVALID")
    _id(terms["merchant_id"], "merchant_id")
    _id(terms["order_id"], "order_id")
    if terms["currency"] != "USD":
        raise LakeMirrorError("fixture currency must be USD", "GATEWAY_INVALID")
    _amount(terms["face_amount_minor"], "face_amount_minor")
    contracts = terms["contracts"]
    if not isinstance(contracts, list) or len(contracts) != 2:
        raise LakeMirrorError("fixture requires two settlement contracts", "GATEWAY_INVALID")
    seen_contracts: set[str] = set()
    for item in contracts:
        if not isinstance(item, dict) or set(item) != {"id", "basis", "advance_bps", "loss_bearer"}:
            raise LakeMirrorError("settlement contract is malformed", "GATEWAY_INVALID")
        ident = _id(item["id"], "contract id")
        if ident in seen_contracts or item["basis"] not in {"dual_replica", "primary_gateway"}:
            raise LakeMirrorError("settlement contract identity or basis is invalid", "GATEWAY_INVALID")
        seen_contracts.add(ident)
        bps = item["advance_bps"]
        if isinstance(bps, bool) or not isinstance(bps, int) or not 0 <= bps <= 10000:
            raise LakeMirrorError("advance_bps is invalid", "GATEWAY_INVALID")
        if item["loss_bearer"] not in {"merchant", "platform"}:
            raise LakeMirrorError("loss bearer is invalid", "GATEWAY_INVALID")

    raw = _load_inside(root, receipts_path)
    if not isinstance(raw, dict) or set(raw) != {"version", "receipts"} or raw["version"] != 1:
        raise LakeMirrorError("gateway receipt envelope is malformed", "GATEWAY_INVALID")
    receipts = raw["receipts"]
    if not isinstance(receipts, list) or not 1 <= len(receipts) <= 128:
        raise LakeMirrorError("gateway receipt count is invalid", "GATEWAY_INVALID")
    seen: set[str] = set()
    captures: dict[str, dict[str, Any]] = {}
    times: dict[str, Any] = {}
    for receipt in receipts:
        if not isinstance(receipt, dict) or set(receipt) != RECEIPT_KEYS:
            raise LakeMirrorError("gateway receipt fields are malformed", "GATEWAY_INVALID")
        ident = _id(receipt["receipt_id"], "receipt_id")
        if ident in seen:
            raise LakeMirrorError("duplicate gateway receipt id", "GATEWAY_DUPLICATE")
        seen.add(ident)
        if (_id(receipt["order_id"], "order_id") != terms["order_id"] or
                receipt["currency"] != terms["currency"] or
                receipt["source"] != "mock-gateway"):
            raise LakeMirrorError("gateway receipt owner or source differs", "GATEWAY_OWNER")
        _amount(receipt["amount_minor"], "amount_minor")
        if receipt["kind"] not in {"capture", "chargeback"}:
            raise LakeMirrorError("gateway receipt kind is invalid", "GATEWAY_INVALID")
        effective = _time(receipt["effective_at"], "effective_at")
        observed = _time(receipt["observed_at"], "observed_at")
        if observed < effective:
            raise LakeMirrorError("receipt observed before effective time", "GATEWAY_INVALID")
        times[ident] = (effective, observed)
        if receipt["kind"] == "capture":
            if receipt["ref_receipt_id"] != "":
                raise LakeMirrorError("capture cannot reference another receipt", "GATEWAY_INVALID")
            captures[ident] = receipt
        elif not isinstance(receipt["ref_receipt_id"], str):
            raise LakeMirrorError("chargeback reference must be a receipt id", "GATEWAY_INVALID")
    for receipt in receipts:
        if receipt["kind"] == "chargeback":
            parent = captures.get(receipt["ref_receipt_id"])
            if parent is None or receipt["amount_minor"] > parent["amount_minor"]:
                raise LakeMirrorError("chargeback needs a matching bounded capture", "GATEWAY_REFERENCE")
            if times[receipt["receipt_id"]][0] < times[parent["receipt_id"]][0]:
                raise LakeMirrorError("chargeback predates capture", "GATEWAY_REFERENCE")
    return {
        "as_of": cutoff.isoformat(), "terms": terms,
        "receipt_fingerprint": fingerprint(raw),
        "visible": [item for item in receipts if times[item["receipt_id"]][1] <= cutoff],
        "future_receipt_ids": [item["receipt_id"] for item in receipts
                               if times[item["receipt_id"]][1] > cutoff],
    }
