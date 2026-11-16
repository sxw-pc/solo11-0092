"""Bounded physical-evidence reader for local order and event rows."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .manifest import load_manifest
from .snapshot import load_chain
from .util import LakeMirrorError, file_facts, fingerprint, read_json, safe_join

FIELDS = {
    "orders": ("order_id", "state", "created_at", "updated_at", "dt"),
    "events": ("event_id", "order_id", "kind", "effective_at", "observed_at", "day"),
}


def read_table_rows(side_root: str | Path, table: str) -> list[dict[str, str]]:
    """Return rows only when pointer, schema, files, and row counts are trusted."""
    if table not in FIELDS:
        raise LakeMirrorError(f"unsupported row table {table}", "ROW_TABLE_UNSUPPORTED")
    root = safe_join(Path(side_root), f"tables/{table}")
    manifest_path = safe_join(root, "_manifest.json", must_exist=True)
    safe_join(root, "_pointer.json", must_exist=True)
    manifest = load_manifest(manifest_path, strict=True)
    historical_path = safe_join(root, f"_snapshots/{manifest.snapshot_id}.json", must_exist=True)
    history_dir = safe_join(root, "_snapshots")
    for item in history_dir.glob("*.json"):
        safe_join(root, f"_snapshots/{item.name}", must_exist=True)
    if fingerprint(read_json(manifest_path)) != fingerprint(read_json(historical_path)):
        raise LakeMirrorError(f"{table} active manifest differs from snapshot history", "ROW_SNAPSHOT_UNSAFE")
    chain = load_chain(root, table, "row-contract")
    if chain.broken or chain.forked or chain.pointer != manifest.snapshot_id:
        raise LakeMirrorError(f"{table} snapshot pointer or chain is unsafe", "ROW_SNAPSHOT_UNSAFE")
    if manifest.table != table or tuple(field.name for field in manifest.fields) != FIELDS[table]:
        raise LakeMirrorError(f"{table} schema does not match row contract", "ROW_SCHEMA_UNSAFE")
    if manifest.partition_spec.columns != (("dt",) if table == "orders" else ("day",)):
        raise LakeMirrorError(f"{table} partition spec does not match row contract", "ROW_SCHEMA_UNSAFE")
    rows: list[dict[str, str]] = []
    for entry in manifest.files:
        path = safe_join(root, entry.path, must_exist=True)
        if path.stat().st_size > 1024 * 1024 or len(rows) > 10000:
            raise LakeMirrorError("row fixture exceeds bounded reader", "ROW_LIMIT")
        facts = file_facts(path)
        if any(facts[key] != getattr(entry, key) for key in ("size", "sha256", "row_count")):
            raise LakeMirrorError(f"{table} file facts differ: {entry.path}", "ROW_FILE_UNTRUSTED")
        try:
            if table == "orders" and path.suffix == ".csv":
                with path.open("r", encoding="utf-8", newline="") as handle:
                    reader = csv.DictReader(handle)
                    if tuple(reader.fieldnames or ()) != FIELDS[table]:
                        raise ValueError("CSV header differs")
                    batch: list[dict[str, Any]] = list(reader)
            elif table == "events" and path.suffix == ".jsonl":
                with path.open("r", encoding="utf-8") as handle:
                    batch = [json.loads(line) for line in handle if line.strip()]
            else:
                raise ValueError("unsupported row file format")
            if any(not isinstance(row, dict) or set(row) != set(FIELDS[table]) or
                   any(not isinstance(value, str) or not value for value in row.values()) for row in batch):
                raise ValueError("row keys or values differ")
        except (ValueError, csv.Error, UnicodeError, json.JSONDecodeError) as exc:
            raise LakeMirrorError(f"{table} rows cannot be trusted: {exc}", "ROW_PARSE_UNTRUSTED") from exc
        date_field = "dt" if table == "orders" else "day"
        if any(row[date_field] != entry.partition.split("=", 1)[1] for row in batch):
            raise LakeMirrorError(f"{table} row date differs from its declared partition", "ROW_PARTITION_UNTRUSTED")
        rows.extend(batch)
    if len(rows) > 10000:
        raise LakeMirrorError("row fixture exceeds bounded reader", "ROW_LIMIT")
    return rows
