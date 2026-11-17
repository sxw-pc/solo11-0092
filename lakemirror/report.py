"""Deterministic JSON, Markdown, and self-contained HTML reports."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .ledger import Ledger
from .report_html import render_html
from .util import LakeMirrorError, pretty_json

BOUNDARIES = [
    "The executor writes only inside the configured replica sandbox under workdir.",
    "No network, cloud, database, or foreign-cluster connection is used.",
    "Manual-review operations are never executed automatically.",
    "Every mutating batch has a local backup directory and a rollback record.",
    "The primary lake directory is read-only to this tool.",
]


class ReportError(LakeMirrorError):
    pass


def build_report(ledger: Ledger, run_id: str = "latest") -> dict[str, Any]:
    run = ledger.get_run(run_id)
    if run is None:
        raise ReportError(f"run not found: {run_id}", "REPORT_RUN_MISSING")
    scan_id = run["run_id"]
    diffs = ledger.get_diffs(scan_id)
    snapshots = ledger.get_snapshots(scan_id)
    settings = ledger.get_settings(scan_id)
    plan = ledger.get_plan_by_scan(scan_id)
    plans = [plan] if plan else []
    batches = [item for item in ledger.list_batches(limit=1000) if plan and item["plan_id"] == plan["plan_id"]]
    events = ledger.get_ledger(run_id=scan_id, limit=1000)
    for batch in batches:
        events.extend(ledger.get_ledger(batch_id=batch["batch_id"], limit=1000))
    unique = {item["event_id"]: item for item in events}
    events = [unique[key] for key in sorted(unique)]
    stats = run.get("stats", {})
    report = {
        "schema_version": 1,
        "project": "lake-mirror",
        "run": {
            "run_id": run["run_id"],
            "status": run["status"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "config_fingerprint": run["config_fingerprint"],
            "config_path": run["config_path"],
            "primary_root": run["primary_root"],
            "replica_root": run["replica_root"],
        },
        "summary": {
            "tables": stats.get("tables", 0),
            "total_diffs": stats.get("total_diffs", len(diffs)),
            "active_diffs": stats.get("active_diffs", sum(1 for item in diffs if not item["ignored"])),
            "auto_fixable_diffs": stats.get("auto_fixable_diffs", sum(1 for item in diffs if item["auto_fixable"] and not item["ignored"])),
            "manual_review_diffs": stats.get("manual_review_diffs", sum(1 for item in diffs if not item["auto_fixable"] and not item["ignored"])),
            "ignored_count": stats.get("ignored_count", sum(1 for item in diffs if item["ignored"])),
            "counts_by_code": stats.get("counts_by_code", {}),
            "counts_by_severity": stats.get("counts_by_severity", {}),
        },
        "tables": stats.get("table_stats", {}),
        "ignore_rules": settings.get("ignore_rules", []),
        "diffs": diffs,
        "snapshots": snapshots,
        "plans": plans,
        "batches": batches,
        "ledger": events,
        "boundaries": BOUNDARIES,
    }
    return report


def render_json(report: dict[str, Any]) -> str:
    return pretty_json(report)


def _md(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> str:
    run = report["run"]
    summary = report["summary"]
    lines = [
        "# Lake Mirror reconciliation report",
        "",
        "Local dual-replica lake consistency, with a deterministic repair audit trail.",
        "",
        f"- Run: `{run['run_id']}`",
        f"- Status: **{run['status']}**",
        f"- Started: `{run['started_at']}`",
        f"- Finished: `{run['finished_at']}`",
        f"- Config fingerprint: `{run['config_fingerprint']}`",
        "",
        "## Summary",
        "",
        f"- Tables: {summary['tables']}",
        f"- Conflicts: {summary['active_diffs']} active / {summary['total_diffs']} total",
        f"- Auto-fixable: {summary['auto_fixable_diffs']}",
        f"- Manual review: {summary['manual_review_diffs']}",
        f"- Ignored: {summary['ignored_count']}",
        "",
    ]
    if summary["counts_by_code"]:
        lines.extend(["| Conflict code | Count |", "| --- | ---: |"])
        for code, count in sorted(summary["counts_by_code"].items()):
            lines.append(f"| `{_md(code)}` | {count} |")
        lines.append("")
    lines.extend(["## Ignore rules", ""])
    if report["ignore_rules"]:
        lines.extend(["| Code | Table | Path prefix | Reason |", "| --- | --- | --- | --- |"])
        for rule in report["ignore_rules"]:
            lines.append(
                f"| `{_md(rule.get('code', '*'))}` | `{_md(rule.get('table', '*'))}` | "
                f"`{_md(rule.get('path_prefix', ''))}` | {_md(rule.get('reason', ''))} |"
            )
    else:
        lines.append("No ignore rules are configured.")
    lines.extend(["", "## Active conflicts", ""])
    active = [item for item in report["diffs"] if not item["ignored"]]
    if active:
        lines.extend(["| Severity | Code | Table | Partition | Path | Expected | Actual |", "| --- | --- | --- | --- | --- | --- | --- |"])
        for item in active:
            lines.append(
                f"| {_md(item['severity'])} | `{_md(item['code'])}` | `{_md(item['table'])}` | "
                f"`{_md(item['partition'])}` | `{_md(item['path'])}` | `{_md(item['expected'])}` | `{_md(item['actual'])}` |"
            )
    else:
        lines.append("No active conflicts.")
    lines.extend(["", "## Ignored conflicts (audited, not repaired)", ""])
    ignored = [item for item in report["diffs"] if item["ignored"]]
    if ignored:
        for item in ignored:
            lines.append(f"- `{item['code']}` on `{item['table']}` `{item['partition']}` `{item['path']}`: {item['ignore_reason']}")
    else:
        lines.append("None.")
    lines.extend(["", "## Snapshot chain", ""])
    if report["snapshots"]:
        lines.extend(["| Side | Table | Snapshot | Sequence | Parent | Files | Bytes |", "| --- | --- | --- | ---: | --- | ---: | ---: |"])
        for snap in report["snapshots"]:
            lines.append(
                f"| {snap['side']} | `{snap['table_name']}` | `{snap['snapshot_id']}` | {snap['sequence']} | "
                f"`{snap['parent_snapshot_id'] or '-'}` | {snap['file_count']} | {snap['total_bytes']} |"
            )
    else:
        lines.append("No snapshot metadata recorded.")
    lines.extend(["", "## Repair plan", ""])
    if report["plans"]:
        for plan in report["plans"]:
            lines.append(f"Plan `{plan['plan_id']}` ({plan['kind']}), operations: {plan['operation_count']}, manual: {plan['manual_review_count']}.")
            lines.extend(["", "| # | Type | Table | Partition | Path | Auto | Reason |", "| ---: | --- | --- | --- | --- | --- | --- |"])
            for index, operation in enumerate(plan["operations"], 1):
                lines.append(
                    f"| {index} | `{operation['type']}` | `{_md(operation.get('table', ''))}` | "
                    f"`{_md(operation.get('partition', ''))}` | `{_md(operation.get('path', ''))}` | "
                    f"{'yes' if operation.get('auto_executable') else 'no'} | {_md(operation.get('reason', ''))} |"
                )
    else:
        lines.append("No plan has been generated for this run.")
    lines.extend(["", "## Execution batches", ""])
    if report["batches"]:
        lines.extend(["| Batch | Status | Backups | Started | Finished |", "| --- | --- | --- | --- | --- |"])
        for batch in report["batches"]:
            lines.append(
                f"| `{batch['batch_id']}` | {batch['status']} | `{_md(batch['backup_dir'] or '-')}` | "
                f"`{batch['started_at']}` | `{batch['finished_at'] or '-'}` |"
            )
    else:
        lines.append("No execution batch recorded.")
    lines.extend(
        [
            "",
            "## Operations",
            "",
            "Dry-run: `python -m lakemirror apply --plan plan.json --workdir ./work`",
            "",
            "Execute: `python -m lakemirror apply --plan plan.json --workdir ./work --execute`",
            "",
            "Verify: `python -m lakemirror verify --batch-id latest --workdir ./work`",
            "",
            "Rollback: `python -m lakemirror rollback --batch-id latest --workdir ./work --execute`",
            "",
            "## Safety boundaries",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in report["boundaries"])
    return "\n".join(lines) + "\n"


def render_report(report: dict[str, Any], fmt: str) -> str:
    if fmt == "json":
        return render_json(report)
    if fmt == "md":
        return render_markdown(report)
    if fmt == "html":
        return render_html(report)
    raise ReportError(f"unsupported report format: {fmt}", "REPORT_FORMAT")


def write_report(ledger: Ledger, run_id: str, fmt: str, output: str | Path) -> Path:
    content = render_report(build_report(ledger, run_id), fmt)
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path
