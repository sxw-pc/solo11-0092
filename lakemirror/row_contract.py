"""As-of order lifecycle contract across local orders and events tables."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from .row_reader import read_table_rows
from .util import LakeMirrorError, parse_timestamp, read_json, within

TRANSITIONS = {(None, "create"): "created", ("created", "pay"): "paid",
               ("created", "cancel"): "cancelled", ("paid", "refund"): "refunded"}


def _time(value: Any, field: str):
    if not isinstance(value, str) or parse_timestamp(value) is None:
        raise LakeMirrorError(f"{field} must be an ISO timestamp", "ROW_CONTRACT_INVALID")
    return parse_timestamp(value)


def _load_contract(workdir: Path, path: str | Path) -> dict[str, Any]:
    file = Path(path).resolve()
    if not within(workdir, file) or not file.is_file() or file.stat().st_size > 1024 * 1024:
        raise LakeMirrorError("row contract must be a bounded file inside workdir", "ROW_CONTRACT_PATH")
    value = read_json(file)
    if not isinstance(value, dict) or set(value) != {"version", "as_of", "effective_through"} or value["version"] != 1:
        raise LakeMirrorError("row contract fields or version differ", "ROW_CONTRACT_INVALID")
    as_of, through = _time(value["as_of"], "as_of"), _time(value["effective_through"], "effective_through")
    if through > as_of:
        raise LakeMirrorError("effective_through cannot exceed as_of", "ROW_CONTRACT_INVALID")
    return value


def _evaluate_rows(orders: list[dict[str, str]], events: list[dict[str, str]],
                   as_of: Any, through: Any) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    by_order: dict[str, dict[str, str]] = {}
    for order in orders:
        key = order["order_id"]
        if key in by_order:
            issues.append({"code": "DUPLICATE_ORDER", "order_id": key})
        by_order[key] = order
    visible: dict[str, list[tuple[Any, dict[str, str]]]] = defaultdict(list)
    future: set[str] = set()
    seen_events: set[str] = set()
    for event in events:
        effective = parse_timestamp(event["effective_at"])
        observed = parse_timestamp(event["observed_at"])
        if effective is None or observed is None:
            issues.append({"code": "EVENT_TIME_INVALID", "order_id": event["order_id"]})
            continue
        if effective > through:
            continue
        if observed > as_of:
            future.add(event["order_id"])
            continue
        if event["event_id"] in seen_events:
            issues.append({"code": "DUPLICATE_EVENT", "order_id": event["order_id"]})
        seen_events.add(event["event_id"])
        if event["order_id"] not in by_order:
            issues.append({"code": "ORPHAN_EVENT", "order_id": event["order_id"]})
        visible[event["order_id"]].append((effective, event))
    decisions = []
    for key, order in sorted(by_order.items()):
        created = parse_timestamp(order["created_at"])
        updated = parse_timestamp(order["updated_at"])
        if created is None or updated is None or updated < created or order["state"] not in {"created", "paid", "cancelled", "refunded"}:
            decisions.append({"order_id": key, "status": "inconsistent", "reason": "ORDER_STATE_INVALID"})
            continue
        if created > through:
            decisions.append({"order_id": key, "status": "pending", "reason": "ORDER_NOT_DUE"})
            continue
        timeline = sorted(visible.get(key, []), key=lambda item: (item[0], item[1]["event_id"]))
        if any(a[0] == b[0] for a, b in zip(timeline, timeline[1:])):
            decisions.append({"order_id": key, "status": "unknown", "reason": "EVENT_ORDER_AMBIGUOUS"})
            continue
        state, last_time, fault = None, None, None
        for effective, event in timeline:
            if effective < created:
                fault = "EVENT_BEFORE_ORDER"
                break
            state = TRANSITIONS.get((state, event["kind"]))
            if state is None:
                fault = "LIFECYCLE_INVALID"
                break
            last_time = effective
        if fault:
            decisions.append({"order_id": key, "status": "inconsistent", "reason": fault})
        elif state == order["state"] and last_time == updated:
            decisions.append({"order_id": key, "status": "verified", "reason": "EVENTS_SUPPORT_ORDER"})
        elif key in future:
            decisions.append({"order_id": key, "status": "pending", "reason": "LATE_EVIDENCE_NOT_VISIBLE"})
        else:
            decisions.append({"order_id": key, "status": "inconsistent", "reason": "ORDER_EVENT_STATE_MISMATCH"})
    statuses = {item["status"] for item in decisions}
    status = ("inconsistent" if issues or "inconsistent" in statuses else
              "unknown" if "unknown" in statuses else "pending" if "pending" in statuses else "verified")
    return {"status": status, "orders": decisions, "issues": issues}


def evaluate_row_contract(workdir: str | Path, contract_path: str | Path,
                          as_of_override: str | None = None) -> dict[str, Any]:
    """Distinguish physical uncertainty, temporal pending evidence, and contradiction."""
    root = Path(workdir).resolve()
    contract = _load_contract(root, contract_path)
    as_of = _time(as_of_override, "as_of override") if as_of_override else _time(contract["as_of"], "as_of")
    through = _time(contract["effective_through"], "effective_through")
    if through > as_of:
        raise LakeMirrorError("effective_through cannot exceed as_of", "ROW_CONTRACT_INVALID")
    sides = {}
    for side in ("primary", "replica"):
        side_root = root / "lake" / side
        try:
            orders = read_table_rows(side_root, "orders")
            events = read_table_rows(side_root, "events")
            sides[side] = _evaluate_rows(orders, events, as_of, through)
        except (LakeMirrorError, OSError) as exc:
            sides[side] = {"status": "unknown", "orders": [], "issues": [],
                           "evidence_error": getattr(exc, "code", "ROW_IO_ERROR")}
    primary, replica = sides["primary"]["status"], sides["replica"]["status"]
    consumer = ("unknown" if "unknown" in (primary, replica) else
                "blocked" if "inconsistent" in (primary, replica) else
                "pending" if "pending" in (primary, replica) else "ready")
    return {"as_of": as_of.isoformat(), "effective_through": through.isoformat(),
            "consumer_status": consumer, "sides": sides}
