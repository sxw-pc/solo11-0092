"""Pure constraint reasoning for bounded threshold queries.

This module only reasons over values that the caller already verified: the
known order/refund rows and the trusted missing-slot declarations.  It never
reads files and never fabricates a refund row for a missing slot.
"""

from __future__ import annotations

import re
from typing import Any

from .util import LakeMirrorError

_AMOUNT = re.compile(r"^-?\d+$")
_MAX_SLOTS = 24


class ThresholdError(LakeMirrorError):
    """Raised when query input is inconsistent, untrusted, or infeasible."""


def reject(message: str, code: str = "THRESHOLD_INPUT") -> None:
    raise ThresholdError(message, code)


def _amount(value: Any, where: str) -> int:
    if not isinstance(value, str) or not _AMOUNT.fullmatch(value):
        reject(f"{where}: amount is not an integer", "THRESHOLD_ROWS")
    number = int(value)
    if number < 0:
        reject(f"{where}: negative amount is not allowed", "THRESHOLD_CONSTRAINT")
    return number


def order_values(rows: list[dict[str, str]]) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """Validate verified order rows and return their amounts and a stable view."""

    amounts: dict[str, int] = {}
    output: list[dict[str, Any]] = []
    for row in rows:
        order_id = row["order_id"]
        if not order_id or order_id.strip() != order_id:
            reject("order_id must be a non-empty unspaced string", "THRESHOLD_IDENTITY")
        if order_id in amounts:
            reject(f"duplicate order identity: {order_id}", "THRESHOLD_IDENTITY")
        amount = _amount(row["amount"], f"orders[{order_id}]")
        amounts[order_id] = amount
        output.append({"order_id": order_id, "amount": amount})
    if not amounts:
        reject("the orders side is empty", "THRESHOLD_ROWS")
    return amounts, output


def refund_values(
    rows: list[dict[str, str]], order_amounts: dict[str, int]
) -> tuple[set[str], set[str], list[dict[str, Any]]]:
    """Validate verified refund rows against FK, uniqueness, and amount rules."""

    identities: set[str] = set()
    refunded_orders: set[str] = set()
    output: list[dict[str, Any]] = []
    for row in rows:
        refund_id, order_id = row["refund_id"], row["order_id"]
        if not refund_id or refund_id.strip() != refund_id:
            reject("refund_id must be a non-empty unspaced string", "THRESHOLD_IDENTITY")
        if refund_id in identities:
            reject(f"duplicate refund identity: {refund_id}", "THRESHOLD_IDENTITY")
        if order_id not in order_amounts:
            reject(f"refund references an unknown order: {order_id}", "THRESHOLD_CONSTRAINT")
        amount = _amount(row["amount"], f"refunds[{refund_id}]")
        if amount > order_amounts[order_id]:
            reject(f"refund exceeds its order amount: {refund_id}", "THRESHOLD_CONSTRAINT")
        if order_id in refunded_orders:
            reject(f"multiple refunds for one order: {order_id}", "THRESHOLD_CONSTRAINT")
        identities.add(refund_id)
        refunded_orders.add(order_id)
        output.append({"refund_id": refund_id, "order_id": order_id, "amount": amount})
    return identities, refunded_orders, output


def missing_slots(
    stats: dict[str, Any],
    missing: list[dict[str, Any]],
    order_amounts: dict[str, int],
    known_ids: set[str],
    refunded_orders: set[str],
) -> list[dict[str, Any]]:
    """Validate trusted missing-slot metadata and bind it to missing files."""

    for key in ("one_refund_per_order", "refund_never_exceeds_order_amount"):
        if stats.get(key) is not True:
            reject(f"missing-slot contract requires {key}=true", "THRESHOLD_CONSTRAINT")
    raw = stats.get("missing_slots", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        reject("stats.missing_slots must be an array", "THRESHOLD_MISSING_METADATA")
    missing_paths = {item["path"] for item in missing}
    if not raw:
        if any(item["row_count"] for item in missing):
            reject("missing file rows have no declared slots", "THRESHOLD_MISSING_METADATA")
        return []
    if not missing:
        reject("missing slots are declared without a missing file", "THRESHOLD_MISSING_METADATA")
    if len(raw) > _MAX_SLOTS:
        reject("too many missing slots for a bounded proof", "THRESHOLD_CONSTRAINT")
    default_file = missing[0]["path"] if len(missing) == 1 else None
    slots: list[dict[str, Any]] = []
    seen = set(known_ids)
    per_file: dict[str, int] = {}
    for index, item in enumerate(raw):
        where = f"stats.missing_slots[{index}]"
        allowed = {"slot", "eligible_order_ids", "max_amount", "file"}
        if not isinstance(item, dict) or set(item) - allowed:
            reject(f"{where}: untrusted slot object", "THRESHOLD_MISSING_METADATA")
        slot_id = item.get("slot")
        if not isinstance(slot_id, str) or not slot_id or slot_id.strip() != slot_id:
            reject(f"{where}: slot identity is invalid", "THRESHOLD_IDENTITY")
        if slot_id in seen:
            reject(f"duplicate refund identity: {slot_id}", "THRESHOLD_IDENTITY")
        seen.add(slot_id)
        eligible = item.get("eligible_order_ids")
        if not isinstance(eligible, list) or not eligible or any(
            not isinstance(value, str) or not value for value in eligible
        ):
            reject(f"{where}: eligible_order_ids must be non-empty strings", "THRESHOLD_MISSING_METADATA")
        if len(set(eligible)) != len(eligible):
            reject(f"{where}: duplicate candidate order", "THRESHOLD_MISSING_METADATA")
        unknown = sorted(set(eligible) - set(order_amounts))
        if unknown:
            reject(f"{where}: candidate order does not exist: {unknown[0]}", "THRESHOLD_CONSTRAINT")
        max_amount = item.get("max_amount")
        if type(max_amount) is not int or max_amount < 0:
            reject(f"{where}: max_amount must be a non-negative integer", "THRESHOLD_MISSING_METADATA")
        file_name = item.get("file", default_file)
        if file_name not in missing_paths:
            reject(f"{where}: missing slot is not bound to a missing file", "THRESHOLD_MISSING_METADATA")
        per_file[file_name] = per_file.get(file_name, 0) + 1
        candidates = sorted(order for order in eligible if order not in refunded_orders)
        if not candidates:
            reject(f"{where}: no legal order remains for slot {slot_id}", "THRESHOLD_INFEASIBLE")
        slots.append({
            "slot": slot_id,
            "file": file_name,
            "eligible_order_ids": list(eligible),
            "candidate_order_ids": candidates,
            "max_amount": max_amount,
        })
    for item in missing:
        if per_file.get(item["path"], 0) != item["row_count"]:
            reject(f"missing slot count differs for {item['path']}", "THRESHOLD_MISSING_METADATA")
    return slots


def worst_case(slots: list[dict[str, Any]], order_amounts: dict[str, int]) -> tuple[int, list[dict[str, Any]]]:
    """Maximum refund total over all legal injective slot-to-order completions."""

    best_total = -1
    best: list[tuple[str, int]] | None = None
    chosen: list[tuple[str, int]] = []

    def walk(index: int, used: frozenset[str], total: int) -> None:
        nonlocal best_total, best
        if index == len(slots):
            if total > best_total:
                best_total, best = total, list(chosen)
            return
        slot = slots[index]
        for order_id in slot["candidate_order_ids"]:
            if order_id in used:
                continue
            cap = min(slot["max_amount"], order_amounts[order_id])
            chosen.append((order_id, cap))
            walk(index + 1, used | {order_id}, total + cap)
            chosen.pop()

    walk(0, frozenset(), 0)
    if best is None:
        reject("no legal completion satisfies the missing-slot constraints", "THRESHOLD_INFEASIBLE")
    witness = [
        {
            "slot": slot["slot"],
            "order_id": order_id,
            "amount": amount,
            "slot_max_amount": slot["max_amount"],
            "order_amount": order_amounts[order_id],
        }
        for slot, (order_id, amount) in zip(slots, best)
    ]
    return best_total, witness


def slot_domains(slots: list[dict[str, Any]], order_amounts: dict[str, int]) -> list[dict[str, Any]]:
    return [
        {
            "slot": slot["slot"],
            "order_ids": list(slot["candidate_order_ids"]),
            "max_amount_by_order": {
                order_id: min(slot["max_amount"], order_amounts[order_id])
                for order_id in slot["candidate_order_ids"]
            },
        }
        for slot in slots
    ]
