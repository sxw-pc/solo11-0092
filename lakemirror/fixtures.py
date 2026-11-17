"""Deterministic offline fixture generation for local lake replica scenarios."""

from __future__ import annotations

import csv
import io
import json
import random
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .util import LakeMirrorError, atomic_write_bytes, pretty_json, sha256_bytes, within

SCENARIOS = ("clean_pair", "drift_basic", "schema_drift", "snapshot_lag")
ORDERS_PATH = "tables/orders"
EVENTS_PATH = "tables/events"


@dataclass(frozen=True)
class DataFile:
    rel: str
    partition: str
    content: bytes
    row_count: int


def _orders_schema() -> list[dict[str, Any]]:
    return [
        {"name": "order_id", "type": "LONG", "nullable": False},
        {"name": "customer_id", "type": "STRING", "nullable": True},
        {"name": "amount", "type": "DECIMAL", "nullable": True},
        {"name": "dt", "type": "STRING", "nullable": False},
    ]


def _events_schema() -> list[dict[str, Any]]:
    return [
        {"name": "event_id", "type": "STRING", "nullable": False},
        {"name": "kind", "type": "STRING", "nullable": False},
        {"name": "payload", "type": "STRING", "nullable": True},
        {"name": "day", "type": "STRING", "nullable": False},
    ]


def _csv(rows: list[list[str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["order_id", "customer_id", "amount", "dt"])
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _jsonl(rows: list[dict[str, Any]]) -> bytes:
    return ("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows)).encode("utf-8")


def _orders_file(seed: int, rel: str, dt: str, count: int, variant: int = 0) -> DataFile:
    rng = random.Random(seed + variant * 997)
    rows = [
        [str(10000 + index + variant * 100), f"account-{rng.randrange(9):02d}",
         f"{rng.randrange(100, 5000) / 100:.2f}", dt]
        for index in range(count)
    ]
    return DataFile(rel, f"dt={dt}", _csv(rows), count)


def _events_file(seed: int, rel: str, day: str, count: int, variant: int = 0) -> DataFile:
    rng = random.Random(seed + variant * 131)
    rows = [
        {"event_id": f"evt-{day}-{index:03d}", "kind": ("create", "update", "read")[rng.randrange(3)],
         "payload": f"sample-{rng.randrange(10000):04d}", "day": day}
        for index in range(count)
    ]
    return DataFile(rel, f"day={day}", _jsonl(rows), count)


def _base_orders(seed: int) -> list[DataFile]:
    return [
        _orders_file(seed, "data/dt=2024-01-01/part-0001.csv", "2024-01-01", 3),
        _orders_file(seed, "data/dt=2024-01-01/part-0002.csv", "2024-01-01", 2),
        _orders_file(seed, "data/dt=2024-01-02/part-0001.csv", "2024-01-02", 2),
    ]


def _base_events(seed: int) -> list[DataFile]:
    return [
        _events_file(seed, "data/day=2024-02-01/part-0001.jsonl", "2024-02-01", 3),
        _events_file(seed, "data/day=2024-02-02/part-0001.jsonl", "2024-02-02", 2),
    ]


def _manifest(table: str, snapshot_id: str, parent: str | None, sequence: int,
              schema: list[dict[str, Any]], spec: list[str], files: list[dict[str, Any]],
              created_at: str) -> dict[str, Any]:
    stats = {"file_count": len(files), "total_bytes": sum(item["size"] for item in files)}
    if table == "orders" and files:
        stats["order_id"] = {"min": 10000, "max": 10199}
    return {
        "table": table,
        "snapshot_id": snapshot_id,
        "parent_snapshot_id": parent,
        "sequence": sequence,
        "created_at": created_at,
        "schema": schema,
        "partition_spec": spec,
        "files": sorted(files, key=lambda item: (item["partition"], item["path"])),
        "stats": stats,
    }


def _entry(file: DataFile, spec: list[str]) -> dict[str, Any]:
    return {
        "path": file.rel,
        "partition": file.partition,
        "size": len(file.content),
        "row_count": file.row_count,
        "sha256": sha256_bytes(file.content),
    }


def _write_table(root: Path, table: str, schema: list[dict[str, Any]], spec: list[str],
                 files: list[DataFile], snapshot_ids: list[str], pointer: str) -> dict[str, Any]:
    table_root = root.joinpath(*("tables/" + table).split("/"))
    table_root.mkdir(parents=True, exist_ok=True)
    entries = [_entry(item, spec) for item in files]
    for item in files:
        target = table_root.joinpath(*item.rel.split("/"))
        atomic_write_bytes(target, item.content)
    snapshots = []
    for index, snapshot_id in enumerate(snapshot_ids):
        parent = snapshot_ids[index - 1] if index else None
        snapshots.append(
            _manifest(table, snapshot_id, parent, index + 1, schema, spec, entries,
                      f"2024-03-{index + 1:02d}T00:00:00Z")
        )
    snapshots_dir = table_root / "_snapshots"
    snapshots_dir.mkdir()
    for snapshot in snapshots:
        atomic_write_bytes(snapshots_dir / f"{snapshot['snapshot_id']}.json", pretty_json(snapshot).encode("utf-8"))
    atomic_write_bytes(table_root / "_manifest.json", pretty_json(snapshots[-1]).encode("utf-8"))
    atomic_write_bytes(table_root / "_pointer.json", pretty_json({"table": table, "snapshot_id": pointer}).encode("utf-8"))
    return {"snapshot_ids": snapshot_ids, "files": len(files), "bytes": sum(len(item.content) for item in files)}


def _snapshot_ids(table: str, count: int) -> list[str]:
    return [f"snap-{table}-{index:04d}" for index in range(1, count + 1)]


def _scenario_tables(scenario: str, seed: int) -> dict[str, dict[str, Any]]:
    primary_orders, primary_events = _base_orders(seed), _base_events(seed)
    replica_orders = list(primary_orders)
    replica_events = list(primary_events)
    order_schema, event_schema = _orders_schema(), _events_schema()
    replica_order_schema, replica_event_schema = _orders_schema(), _events_schema()
    order_spec, replica_order_spec = ["dt"], ["dt"]
    event_spec, replica_event_spec = ["day"], ["day"]
    primary_snaps, replica_snaps = 2, 2
    primary_pointer, replica_pointer = 2, 2
    if scenario == "drift_basic":
        primary_orders = primary_orders + [_orders_file(seed, "data/dt=2024-01-03/part-0001.csv", "2024-01-03", 1, 8)]
        primary_events = primary_events + [_events_file(seed, "data/day=2024-02-03/part-0001.jsonl", "2024-02-03", 2, 6)]
        replica_orders = [
            primary_orders[1],
            _orders_file(seed, "data/dt=2024-01-02/part-0001.csv", "2024-01-02", 1, 3),
            _orders_file(seed, "data/dt=2024-01-01/part-9999.csv", "2024-01-01", 1, 4),
        ]
        replica_events = [
            _events_file(seed, "data/day=2024-02-01/part-0001.jsonl", "2024-02-01", 3, 5),
            primary_events[1],
            _events_file(seed, "data/day=2024-02-09/part-0001.jsonl", "2024-02-09", 1, 9),
        ]
        replica_pointer = 1
    elif scenario == "schema_drift":
        replica_order_schema = [
            {"name": "order_id", "type": "LONG", "nullable": True},
            {"name": "amount", "type": "STRING", "nullable": True},
            {"name": "dt", "type": "STRING", "nullable": False},
            {"name": "note", "type": "STRING", "nullable": True},
        ]
        replica_order_spec = ["dt"]
        replica_event_schema = _events_schema()
        replica_event_spec = ["day", "shard"]
        replica_events = [
            DataFile(item.rel, f"day={item.partition.split('=', 1)[1]},shard=0", item.content, item.row_count)
            for item in primary_events
        ]
    elif scenario == "snapshot_lag":
        primary_snaps, replica_snaps = 3, 2
        primary_pointer, replica_pointer = 3, 1
    tables = {
        "orders": {
            "primary_path": ORDERS_PATH, "replica_path": ORDERS_PATH,
            "primary_schema": order_schema, "replica_schema": replica_order_schema,
            "primary_spec": order_spec, "replica_spec": replica_order_spec,
            "primary_files": primary_orders, "replica_files": replica_orders,
            "primary_snaps": primary_snaps, "replica_snaps": replica_snaps,
            "primary_pointer": primary_pointer, "replica_pointer": replica_pointer,
        },
        "events": {
            "primary_path": EVENTS_PATH, "replica_path": EVENTS_PATH,
            "primary_schema": event_schema, "replica_schema": replica_event_schema,
            "primary_spec": event_spec, "replica_spec": replica_event_spec,
            "primary_files": primary_events, "replica_files": replica_events,
            "primary_snaps": primary_snaps, "replica_snaps": replica_snaps,
            "primary_pointer": primary_pointer, "replica_pointer": replica_pointer,
        },
    }
    return tables


def _write_samples(lake: Path) -> None:
    sample_dir = lake / "samples"
    sample_dir.mkdir(parents=True, exist_ok=True)
    unsafe = _manifest(
        "unsafe_sample", "snap-unsafe-0001", None, 1,
        [{"name": "id", "type": "LONG", "nullable": False}, {"name": "dt", "type": "STRING", "nullable": False}],
        ["dt"], [{"path": "../../outside.csv", "partition": "dt=2024-01-01", "size": 1,
                  "row_count": 1, "sha256": "0" * 64}], "2024-03-01T00:00:00Z",
    )
    atomic_write_bytes(sample_dir / "path_escape_manifest.json", pretty_json(unsafe).encode("utf-8"))
    spec_files = [{"path": "data/day=2024-02-01/part-0001.jsonl", "partition": "day=2024-02-01",
                   "size": 1, "row_count": 1, "sha256": "1" * 64}]
    primary = _manifest("spec_probe", "snap-spec-0001", None, 1, _events_schema(), ["day"], spec_files, "2024-03-01T00:00:00Z")
    replica = _manifest("spec_probe", "snap-spec-0001", None, 1, _events_schema(), ["day", "shard"], spec_files, "2024-03-01T00:00:00Z")
    atomic_write_bytes(sample_dir / "partition_spec_primary.json", pretty_json(primary).encode("utf-8"))
    atomic_write_bytes(sample_dir / "partition_spec_replica.json", pretty_json(replica).encode("utf-8"))


def generate_fixture(scenario: str, seed: int, workdir: str | Path) -> dict[str, Any]:
    if scenario not in SCENARIOS:
        raise LakeMirrorError(f"unknown scenario {scenario!r}; choose from {', '.join(SCENARIOS)}", "SCENARIO_UNKNOWN")
    root = Path(workdir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    lake = (root / "lake").resolve()
    if not within(root, lake) or lake.name != "lake":
        raise LakeMirrorError("fixture destination is outside workdir", "FIXTURE_PATH_UNSAFE")
    if lake.exists():
        shutil.rmtree(lake)
    primary, replica = lake / "primary", lake / "replica"
    primary.mkdir(parents=True)
    replica.mkdir(parents=True)
    tables = _scenario_tables(scenario, seed)
    report: dict[str, Any] = {"scenario": scenario, "seed": seed, "primary_root": str(primary), "replica_root": str(replica), "tables": {}}
    for name, config in tables.items():
        p_ids = _snapshot_ids(name, config["primary_snaps"])
        r_ids = _snapshot_ids(name, config["replica_snaps"])
        _write_table(primary, name, config["primary_schema"], config["primary_spec"], config["primary_files"],
                     p_ids, p_ids[config["primary_pointer"] - 1])
        _write_table(replica, name, config["replica_schema"], config["replica_spec"], config["replica_files"],
                     r_ids, r_ids[config["replica_pointer"] - 1])
        report["tables"][name] = {
            "primary_snapshots": p_ids,
            "replica_snapshots": r_ids,
            "primary_files": len(config["primary_files"]),
            "replica_files": len(config["replica_files"]),
        }
    _write_samples(lake)
    return report
