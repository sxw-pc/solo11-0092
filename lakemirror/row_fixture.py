"""Deterministic offline order/event rows with temporal contract counterexamples."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from .fixtures import DataFile, _entry, _manifest
from .util import LakeMirrorError, atomic_write_bytes, pretty_json

SCENARIOS = ("consistent", "late_arrival", "orphan", "invalid_timeline", "state_conflict",
             "duplicate_order", "corrupt_file")
DAY = "2024-03-01"
ORDERS_FIELDS = ("order_id", "state", "created_at", "updated_at", "dt")
EVENTS_FIELDS = ("event_id", "order_id", "kind", "effective_at", "observed_at", "day")


def _orders() -> list[dict[str, str]]:
    return [
        {"order_id": "O100", "state": "paid", "created_at": f"{DAY}T08:00:00Z",
         "updated_at": f"{DAY}T09:00:00Z", "dt": DAY},
        {"order_id": "O200", "state": "created", "created_at": f"{DAY}T08:00:00Z",
         "updated_at": f"{DAY}T08:00:00Z", "dt": DAY},
    ]


def _events() -> list[dict[str, str]]:
    return [
        {"event_id": "E100-create", "order_id": "O100", "kind": "create",
         "effective_at": f"{DAY}T08:00:00Z", "observed_at": f"{DAY}T08:10:00Z", "day": DAY},
        {"event_id": "E100-pay", "order_id": "O100", "kind": "pay",
         "effective_at": f"{DAY}T09:00:00Z", "observed_at": f"{DAY}T09:10:00Z", "day": DAY},
        {"event_id": "E200-create", "order_id": "O200", "kind": "create",
         "effective_at": f"{DAY}T08:00:00Z", "observed_at": f"{DAY}T08:10:00Z", "day": DAY},
    ]


def _file(table: str, rows: list[dict[str, str]]) -> DataFile:
    if table == "orders":
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=ORDERS_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        return DataFile(f"data/dt={DAY}/part-0001.csv", f"dt={DAY}", buffer.getvalue().encode(), len(rows))
    content = "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows).encode()
    return DataFile(f"data/day={DAY}/part-0001.jsonl", f"day={DAY}", content, len(rows))


def _table(root: Path, table: str, rows: list[dict[str, str]]) -> None:
    directory = root / "tables" / table
    directory.mkdir(parents=True)
    file = _file(table, rows)
    atomic_write_bytes(directory / file.rel, file.content)
    fields = ORDERS_FIELDS if table == "orders" else EVENTS_FIELDS
    schema = [{"name": name, "type": "STRING", "nullable": False} for name in fields]
    manifest = _manifest(table, f"rows-{table}-0001", None, 1, schema,
                         ["dt"] if table == "orders" else ["day"], [_entry(file, [])],
                         f"{DAY}T10:00:00Z")
    manifest["stats"] = {"file_count": 1, "total_bytes": len(file.content)}
    atomic_write_bytes(directory / "_snapshots" / f"rows-{table}-0001.json", pretty_json(manifest).encode())
    atomic_write_bytes(directory / "_manifest.json", pretty_json(manifest).encode())
    atomic_write_bytes(directory / "_pointer.json", pretty_json({"table": table, "snapshot_id": manifest["snapshot_id"]}).encode())


def generate_row_fixture(scenario: str, workdir: str | Path) -> dict[str, Any]:
    if scenario not in SCENARIOS:
        raise LakeMirrorError(f"unknown row scenario {scenario}", "ROW_SCENARIO_UNKNOWN")
    root = Path(workdir).resolve()
    if (root / "lake").exists() or (root / "row_contract.json").exists():
        raise LakeMirrorError("row fixture destination already contains lake or contract", "ROW_FIXTURE_EXISTS")
    replica_orders, replica_events = _orders(), _events()
    if scenario == "late_arrival":
        replica_events[1]["observed_at"] = f"{DAY}T12:00:00Z"
    elif scenario == "orphan":
        replica_events.append({"event_id": "E999-create", "order_id": "O999", "kind": "create",
                               "effective_at": f"{DAY}T08:30:00Z", "observed_at": f"{DAY}T08:40:00Z", "day": DAY})
    elif scenario == "invalid_timeline":
        replica_events[1]["effective_at"] = f"{DAY}T07:00:00Z"
    elif scenario == "state_conflict":
        replica_orders[0]["state"] = "cancelled"
    elif scenario == "duplicate_order":
        replica_orders.append(dict(replica_orders[0]))
    for side, orders, events in (("primary", _orders(), _events()),
                                 ("replica", replica_orders, replica_events)):
        _table(root / "lake" / side, "orders", orders)
        _table(root / "lake" / side, "events", events)
    if scenario == "corrupt_file":
        target = root / "lake" / "replica" / "tables" / "events" / _file("events", replica_events).rel
        atomic_write_bytes(target, b"{broken jsonl\n")
    contract_path = root / "row_contract.json"
    atomic_write_bytes(contract_path, pretty_json({"version": 1, "as_of": f"{DAY}T10:00:00Z",
                                                    "effective_through": f"{DAY}T10:00:00Z"}).encode())
    return {"scenario": scenario, "workdir": str(root), "contract": str(contract_path)}
