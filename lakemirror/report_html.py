"""Self-contained, offline HTML rendering for Lake Mirror reports."""

from __future__ import annotations

from html import escape
from typing import Any

_CSS = """
:root { color-scheme: light dark; --ink:#17212b; --muted:#5c6b7a; --line:#ccd5df;
  --bg:#f7f9fb; --card:#ffffff; --accent:#0b6e6e; --warn:#9a5b00; --bad:#a1283a; }
@media (prefers-color-scheme: dark) { :root { --ink:#e7edf3; --muted:#a8b4c0; --line:#384553;
  --bg:#111820; --card:#18222c; --accent:#6cd4d4; --warn:#f3bd66; --bad:#ff8b9b; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 ui-sans-serif, system-ui, sans-serif; }
header, main, footer { max-width:1180px; margin:0 auto; padding:0 22px; }
header { padding-top:32px; }
h1 { margin:0 0 6px; font-size:28px; letter-spacing:-.02em; }
h2 { margin-top:34px; border-bottom:1px solid var(--line); padding-bottom:7px; font-size:19px; }
h3 { margin-top:22px; font-size:16px; }
p.lead { color:var(--muted); margin-top:0; }
.grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(160px, 1fr)); gap:12px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:15px; }
.card b { display:block; font-size:24px; margin-top:3px; }
.muted { color:var(--muted); }
.badge { display:inline-block; padding:2px 8px; border-radius:999px; border:1px solid var(--line); font-size:12px; }
.badge.bad { color:var(--bad); border-color:var(--bad); }
.badge.warn { color:var(--warn); border-color:var(--warn); }
table { width:100%; border-collapse:collapse; background:var(--card); margin:12px 0 20px; }
th, td { text-align:left; border:1px solid var(--line); padding:7px 9px; vertical-align:top; }
th { background:color-mix(in srgb, var(--card) 80%, var(--accent) 20%); }
code { font-family:ui-monospace, SFMono-Regular, Consolas, monospace; font-size:13px; overflow-wrap:anywhere; }
details { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:10px 14px; }
nav a { color:var(--accent); margin-right:14px; }
footer { color:var(--muted); padding:28px 22px 44px; font-size:13px; }
@media print { nav { display:none; } body { background:white; color:black; } }
"""


def _e(value: Any) -> str:
    return escape(str(value), quote=True)


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(f"<th>{_e(item)}</th>" for item in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_e(cell)}</td>" for cell in row) + "</tr>" for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _cards(report: dict[str, Any]) -> str:
    summary = report["summary"]
    values = [
        ("Tables", summary["tables"]),
        ("Active conflicts", summary["active_diffs"]),
        ("Auto-fixable", summary["auto_fixable_diffs"]),
        ("Manual review", summary["manual_review_diffs"]),
        ("Ignored", summary["ignored_count"]),
    ]
    cards = "".join(f'<div class="card"><span class="muted">{_e(name)}</span><b>{_e(value)}</b></div>' for name, value in values)
    return f'<div class="grid">{cards}</div>'


def _conflicts(report: dict[str, Any]) -> str:
    active = [item for item in report["diffs"] if not item["ignored"]]
    if not active:
        return "<p>No active conflicts.</p>"
    rows = [
        [item["severity"], item["code"], item["table"], item["partition"] or "-", item["path"] or "-",
         item["expected"], item["actual"], item["suggestion"]]
        for item in active
    ]
    return _table(["Severity", "Code", "Table", "Partition", "Path", "Expected", "Actual", "Suggested action"], rows)


def _ignored(report: dict[str, Any]) -> str:
    rows = [
        [rule.get("code", "*"), rule.get("table", "*"), rule.get("path_prefix", "") or "*", rule.get("reason", "")]
        for rule in report["ignore_rules"]
    ]
    rules = _table(["Code", "Table", "Path prefix", "Reason"], rows) if rows else "<p>No ignore rules configured.</p>"
    ignored = [item for item in report["diffs"] if item["ignored"]]
    if ignored:
        details = _table(
            ["Code", "Table", "Partition", "Path", "Reason"],
            [[item["code"], item["table"], item["partition"], item["path"], item["ignore_reason"]] for item in ignored],
        )
    else:
        details = "<p>No conflicts were ignored.</p>"
    return rules + "<h3>Ignored conflict evidence</h3>" + details


def _snapshots(report: dict[str, Any]) -> str:
    if not report["snapshots"]:
        return "<p>No snapshot metadata recorded.</p>"
    rows = [
        [item["side"], item["table_name"], item["snapshot_id"], item["sequence"],
         item["parent_snapshot_id"] or "-", item["file_count"], item["total_bytes"]]
        for item in report["snapshots"]
    ]
    return _table(["Side", "Table", "Snapshot", "Sequence", "Parent", "Files", "Bytes"], rows)


def _plans(report: dict[str, Any]) -> str:
    if not report["plans"]:
        return "<p>No plan has been generated for this run.</p>"
    blocks = []
    for plan in report["plans"]:
        rows = [
            [index, item["type"], item.get("table", ""), item.get("partition", ""), item.get("path", ""),
             "yes" if item.get("auto_executable") else "no", item.get("reason", "")]
            for index, item in enumerate(plan["operations"], 1)
        ]
        blocks.append(
            f"<h3><code>{_e(plan['plan_id'])}</code> "
            f"<span class=\"badge\">{_e(plan['kind'])}</span></h3>"
            f"<p class=\"muted\">Operations: {_e(plan['operation_count'])}; manual: {_e(plan['manual_review_count'])}.</p>"
            + _table(["#", "Type", "Table", "Partition", "Path", "Auto", "Reason"], rows)
        )
    return "".join(blocks)


def _batches(report: dict[str, Any]) -> str:
    if not report["batches"]:
        return "<p>No execution batch recorded.</p>"
    rows = [
        [item["batch_id"], item["status"], item["backup_dir"] or "-", item["started_at"], item["finished_at"] or "-"]
        for item in report["batches"]
    ]
    return _table(["Batch", "Status", "Backup directory", "Started", "Finished"], rows)


def _operations(report: dict[str, Any]) -> str:
    return """
<details>
<summary>Safe operation sequence</summary>
<ol>
<li><code>python -m lakemirror scan --config configs/reconcile-basic.json --workdir ./work</code></li>
<li><code>python -m lakemirror plan --scan-id latest --workdir ./work --out ./work/plan.json</code></li>
<li><code>python -m lakemirror apply --plan ./work/plan.json --workdir ./work</code></li>
<li><code>python -m lakemirror apply --plan ./work/plan.json --workdir ./work --execute</code></li>
<li><code>python -m lakemirror verify --batch-id latest --workdir ./work</code></li>
<li><code>python -m lakemirror rollback --batch-id latest --workdir ./work --execute</code></li>
</ol>
<p>Execution is always rejected unless the plan fingerprint, config fingerprint, path boundaries, and source hashes still match.</p>
</details>
"""


def render_html(report: dict[str, Any]) -> str:
    run = report["run"]
    boundaries = "".join(f"<li>{_e(item)}</li>" for item in report["boundaries"])
    payload = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'">
<title>Lake Mirror report {_e(run['run_id'])}</title>
<style>{_CSS}</style>
</head>
<body>
<header>
<p class="badge">local-only / read-only report</p>
<h1>Lake Mirror reconciliation report</h1>
<p class="lead">Dual-copy local lake consistency, deterministic repair plan, and auditable rollback.</p>
<nav><a href="#summary">Summary</a><a href="#conflicts">Conflicts</a><a href="#snapshots">Snapshots</a><a href="#plans">Plans</a><a href="#batches">Batches</a><a href="#boundaries">Safety</a></nav>
</header>
<main>
<section id="summary">
<h2>Run summary</h2>
<p><span class="badge">{_e(run['status'])}</span> <code>{_e(run['run_id'])}</code></p>
<p class="muted">Started {_e(run['started_at'])} · finished {_e(run['finished_at'])} · config <code>{_e(run['config_fingerprint'])}</code></p>
{_cards(report)}
</section>
<section id="conflicts">
<h2>Conflict classification</h2>
{_conflicts(report)}
<h3>Ignore rules and audited exclusions</h3>
{_ignored(report)}
</section>
<section id="snapshots">
<h2>Snapshot chains</h2>
{_snapshots(report)}
</section>
<section id="plans">
<h2>Repair plan</h2>
{_plans(report)}
</section>
<section id="batches">
<h2>Execution and rollback</h2>
{_batches(report)}
{_operations(report)}
</section>
<section id="boundaries">
<h2>Safety boundaries</h2>
<ul>{boundaries}</ul>
</section>
</main>
<footer>Generated offline by Lake Mirror. This document has no external assets, scripts, or network references.</footer>
</body>
</html>
"""
    return payload
