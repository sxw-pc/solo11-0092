"""SQLite audit ledger for scans, plans, executions, and rollbacks."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .reconcile_types import ScanResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
 run_id TEXT PRIMARY KEY, config_fingerprint TEXT NOT NULL, config_path TEXT NOT NULL,
 started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, stats_json TEXT NOT NULL,
 primary_root TEXT NOT NULL, replica_root TEXT NOT NULL, workdir TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
 run_id TEXT NOT NULL, side TEXT NOT NULL, table_name TEXT NOT NULL, snapshot_id TEXT NOT NULL,
 parent_snapshot_id TEXT, sequence INTEGER NOT NULL, created_at TEXT NOT NULL, source TEXT NOT NULL,
 file_count INTEGER NOT NULL, total_bytes INTEGER NOT NULL, issues_json TEXT NOT NULL,
 PRIMARY KEY (run_id, side, table_name, snapshot_id)
);
CREATE TABLE IF NOT EXISTS diffs (
 diff_id TEXT NOT NULL, run_id TEXT NOT NULL, code TEXT NOT NULL, table_name TEXT NOT NULL,
 partition_value TEXT, path TEXT, expected_json TEXT, actual_json TEXT, severity TEXT NOT NULL,
 suggestion TEXT NOT NULL, auto_fixable INTEGER NOT NULL, ignored INTEGER NOT NULL,
 ignore_reason TEXT, evidence_json TEXT NOT NULL, PRIMARY KEY (run_id, diff_id)
);
CREATE TABLE IF NOT EXISTS plans (
 plan_id TEXT PRIMARY KEY, scan_id TEXT NOT NULL, fingerprint TEXT NOT NULL, kind TEXT NOT NULL,
 operation_count INTEGER NOT NULL, manual_review_count INTEGER NOT NULL,
 payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
 operation_id TEXT NOT NULL, plan_id TEXT NOT NULL, op_type TEXT NOT NULL, status TEXT NOT NULL,
 evidence_json TEXT NOT NULL, batch_id TEXT, updated_at TEXT NOT NULL,
 PRIMARY KEY (operation_id, plan_id)
);
CREATE TABLE IF NOT EXISTS batches (
 batch_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, status TEXT NOT NULL, backup_dir TEXT,
 started_at TEXT NOT NULL, finished_at TEXT, summary_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, batch_id TEXT, event TEXT NOT NULL,
 payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings_snapshot (
 run_id TEXT PRIMARY KEY, config_fingerprint TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_diffs_run ON diffs(run_id);
CREATE INDEX IF NOT EXISTS idx_ledger_batch ON ledger(batch_id);
"""


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _load(value: str | None, default: Any) -> Any:
    return default if value is None else json.loads(value)


class Ledger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def save_scan(self, result: ScanResult, started_at: str, finished_at: str, status: str = "completed") -> None:
        stats = dict(result.stats)
        stats["table_stats"] = result.table_stats
        stats["warnings"] = list(result.warnings)
        stats["snapshot_chains"] = [chain.as_dict() for chain in result.chains]
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (result.scan_id, result.config.fingerprint, str(result.config.config_path), started_at,
                 finished_at, status, _dump(stats), str(result.config.primary_root),
                 str(result.config.replica_root), str(result.config.workdir)),
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO settings_snapshot VALUES (?, ?, ?)",
                (result.scan_id, result.config.fingerprint, _dump(result.config.snapshot())),
            )
            for chain in result.chains:
                issues = _dump([item.as_dict() for item in chain.issues])
                for node in chain.nodes:
                    self.conn.execute(
                        "INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (result.scan_id, chain.side, chain.table, node.snapshot_id,
                         node.parent_snapshot_id, node.sequence, node.created_at, chain.side,
                         node.file_count, node.total_bytes, issues),
                    )
            for diff in result.diffs:
                self.conn.execute(
                    "INSERT OR REPLACE INTO diffs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (diff.diff_id, result.scan_id, diff.code, diff.table, diff.partition, diff.path,
                     _dump(diff.expected), _dump(diff.actual), diff.severity, diff.suggestion,
                     int(diff.auto_fixable), int(diff.ignored), diff.ignore_reason, _dump(diff.evidence or {})),
                )
            self.conn.execute(
                "INSERT INTO ledger (run_id, batch_id, event, payload_json, created_at) VALUES (?, NULL, ?, ?, ?)",
                (result.scan_id, "scan_completed", _dump({"diff_count": len(result.diffs)}), finished_at),
            )

    def record_failed_run(self, run_id: str, config_fingerprint: str, config_path: str,
                          started_at: str, finished_at: str, error: str,
                          primary_root: str = "", replica_root: str = "", workdir: str = "") -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, config_fingerprint, config_path, started_at, finished_at, "failed",
                 _dump({"error": error}), primary_root, replica_root, workdir),
            )
            self.conn.execute(
                "INSERT INTO ledger (run_id, batch_id, event, payload_json, created_at) VALUES (?, NULL, ?, ?, ?)",
                (run_id, "scan_failed", _dump({"error": error}), finished_at),
            )

    def _row(self, sql: str, args: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, args).fetchone()

    def get_run(self, run_id: str = "latest") -> dict[str, Any] | None:
        row = self._row("SELECT * FROM runs ORDER BY rowid DESC LIMIT 1") if run_id == "latest" else self._row("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        if row is None:
            return None
        data = dict(row)
        data["stats"] = _load(data.pop("stats_json"), {})
        return data

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM runs ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["stats"] = _load(item.pop("stats_json"), {})
            output.append(item)
        return output

    def get_diffs(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM diffs WHERE run_id = ? ORDER BY diff_id", (run_id,)).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["table"] = item.pop("table_name")
            item["partition"] = item.pop("partition_value") or ""
            item["expected"] = _load(item.pop("expected_json"), None)
            item["actual"] = _load(item.pop("actual_json"), None)
            item["evidence"] = _load(item.pop("evidence_json"), {})
            item["auto_fixable"] = bool(item["auto_fixable"])
            item["ignored"] = bool(item["ignored"])
            output.append(item)
        return output

    def get_snapshots(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM snapshots WHERE run_id = ? ORDER BY side, table_name, sequence", (run_id,)
        ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["issues"] = _load(item.pop("issues_json"), [])
            output.append(item)
        return output

    def get_settings(self, run_id: str) -> dict[str, Any]:
        row = self._row("SELECT payload_json FROM settings_snapshot WHERE run_id = ?", (run_id,))
        return {} if row is None else _load(row["payload_json"], {})

    def save_plan(self, plan: dict[str, Any], created_at: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO plans VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (plan["plan_id"], plan["scan_id"], plan["plan_fingerprint"], plan["kind"],
                 plan["operation_count"], plan["manual_review_count"], _dump(plan), created_at),
            )
            self.conn.execute("DELETE FROM operations WHERE plan_id = ?", (plan["plan_id"],))
            for operation in plan["operations"]:
                self.conn.execute(
                    "INSERT INTO operations VALUES (?, ?, ?, ?, ?, NULL, ?)",
                    (operation["op_id"], plan["plan_id"], operation["type"], "pending", _dump(operation), created_at),
                )

    def get_plan(self, plan_id: str = "latest") -> dict[str, Any] | None:
        row = self._row("SELECT payload_json FROM plans ORDER BY rowid DESC LIMIT 1") if plan_id == "latest" else self._row("SELECT payload_json FROM plans WHERE plan_id = ?", (plan_id,))
        return None if row is None else _load(row["payload_json"], {})

    def get_plan_by_scan(self, scan_id: str) -> dict[str, Any] | None:
        row = self._row("SELECT payload_json FROM plans WHERE scan_id = ? ORDER BY rowid DESC LIMIT 1", (scan_id,))
        return None if row is None else _load(row["payload_json"], {})

    def list_plans(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM plans ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
        return [{k: (v if k != "payload_json" else _load(v, {})) for k, v in dict(row).items()} for row in rows]

    def set_operation(self, plan_id: str, batch_id: str | None, operation: dict[str, Any],
                      status: str, evidence: dict[str, Any], updated_at: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO operations VALUES (?, ?, ?, ?, ?, ?, ?)",
                (operation["op_id"], plan_id, operation["type"], status, _dump(evidence), batch_id, updated_at),
            )

    def get_operations(self, plan_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM operations WHERE plan_id = ? ORDER BY operation_id", (plan_id,)
        ).fetchall()
        return [{**dict(row), "evidence": _load(row["evidence_json"], {})} for row in rows]

    def create_batch(self, batch_id: str, plan_id: str, status: str, backup_dir: str | None,
                     started_at: str, summary: dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO batches VALUES (?, ?, ?, ?, ?, NULL, ?)",
                (batch_id, plan_id, status, backup_dir, started_at, _dump(summary)),
            )

    def finish_batch(self, batch_id: str, status: str, finished_at: str, summary: dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE batches SET status = ?, finished_at = ?, summary_json = ? WHERE batch_id = ?",
                (status, finished_at, _dump(summary), batch_id),
            )

    def get_batch(self, batch_id: str = "latest") -> dict[str, Any] | None:
        sql = "SELECT * FROM batches WHERE status != 'dry_run' ORDER BY rowid DESC LIMIT 1" if batch_id == "latest" else "SELECT * FROM batches WHERE batch_id = ?"
        row = self._row(sql if batch_id == "latest" else sql, () if batch_id == "latest" else (batch_id,))
        if row is None:
            return None
        item = dict(row)
        item["summary"] = _load(item.pop("summary_json"), {})
        return item

    def list_batches(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM batches ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["summary"] = _load(item.pop("summary_json"), {})
            output.append(item)
        return output

    def append(self, event: str, payload: dict[str, Any], created_at: str,
               run_id: str | None = None, batch_id: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO ledger (run_id, batch_id, event, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (run_id, batch_id, event, _dump(payload), created_at),
            )

    def get_ledger(self, *, run_id: str | None = None, batch_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        clauses, args = [], []
        if run_id:
            clauses.append("run_id = ?")
            args.append(run_id)
        if batch_id:
            clauses.append("batch_id = ?")
            args.append(batch_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM ledger{where} ORDER BY event_id DESC LIMIT ?", (*args, limit)
        ).fetchall()
        return [{**dict(row), "payload": _load(row["payload_json"], {})} for row in rows]
