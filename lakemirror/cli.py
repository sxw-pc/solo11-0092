"""Command-line interface for fixture generation, reconciliation, execution, and review."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .config import Config, load_config
from .executor import execute_plan
from .fixtures import generate_fixture
from .ledger import Ledger
from .plan_io import load_plan
from .planner import build_plan
from .provenance_evidence import inspect_report_origins
from .provenance_receipts import write_release_artifacts
from .provenance_release import adjudicate_report_release
from .reconcile import scan_config
from .report import build_report, render_report
from .rollback import rollback_batch
from .server import serve_forever
from .util import LakeMirrorError, pretty_json, utc_now, within, write_json
from .verify import verify_batch


def _fail(message: str, code: str = "CLI_ERROR") -> int:
    print(f"error [{code}]: {message}", file=sys.stderr)
    return 2


def _check_workdir(config: Config, raw: str | Path) -> None:
    if Path(raw).resolve() != config.workdir.resolve():
        raise LakeMirrorError(f"--workdir {raw} does not match config workdir {config.workdir}", "WORKDIR_MISMATCH")


def _db_path(workdir: str | Path, raw: str | None, default: Path | None = None) -> Path:
    root = Path(workdir).resolve()
    path = Path(raw).resolve() if raw else (default or (root / "ledger.db")).resolve()
    if not within(root, path):
        raise LakeMirrorError("ledger must stay inside workdir", "LEDGER_PATH_UNSAFE")
    return path


def _config_from_run(run: dict[str, Any], workdir: str | Path) -> Config:
    config = load_config(run["config_path"])
    _check_workdir(config, workdir)
    return config


def _batch_context(ledger: Ledger, batch_id: str, workdir: str | Path) -> tuple[dict[str, Any], Config]:
    batch = ledger.get_batch(batch_id)
    if batch is None:
        raise LakeMirrorError(f"batch not found: {batch_id}", "BATCH_NOT_FOUND")
    plan = ledger.get_plan(batch["plan_id"])
    if plan is None:
        raise LakeMirrorError(f"plan not found for batch: {batch['plan_id']}", "PLAN_NOT_FOUND")
    config = load_config(plan["config_path"])
    _check_workdir(config, workdir)
    return batch, config


def cmd_fixtures(args: argparse.Namespace) -> int:
    report = generate_fixture(args.scenario, args.seed, args.workdir)
    print(pretty_json(report), end="")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    _check_workdir(config, args.workdir)
    db = _db_path(args.workdir, args.db, config.ledger_path)
    scan_id = f"scan-{time.time_ns()}"
    started = utc_now()
    ledger = Ledger(db)
    try:
        try:
            result = scan_config(config, scan_id)
        except Exception as exc:
            ledger.record_failed_run(scan_id, config.fingerprint, str(config.config_path), started,
                                     utc_now(), str(exc), str(config.primary_root),
                                     str(config.replica_root), str(config.workdir))
            raise
        ledger.save_scan(result, started, utc_now())
        output: dict[str, Any] = {
            "scan_id": result.scan_id,
            "db": str(db),
            "stats": result.stats,
            "counts_by_code": result.stats["counts_by_code"],
        }
        if args.report:
            fmt = Path(args.report).suffix.lower().lstrip(".") or "json"
            if fmt not in {"json", "md", "html"}:
                fmt = "json"
            report_text = render_report(build_report(ledger, result.scan_id), fmt)
            Path(args.report).parent.mkdir(parents=True, exist_ok=True)
            Path(args.report).write_text(report_text, encoding="utf-8")
            output["report"] = str(Path(args.report).resolve())
        print(pretty_json(output), end="")
        return 0
    finally:
        ledger.close()


def cmd_plan(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).resolve()
    db = _db_path(workdir, args.db)
    with Ledger(db) as ledger:
        run = ledger.get_run(args.scan_id)
        if run is None:
            raise LakeMirrorError(f"scan not found: {args.scan_id}", "SCAN_NOT_FOUND")
        if run["status"] != "completed":
            raise LakeMirrorError(f"scan status is {run['status']}; cannot plan", "SCAN_INCOMPLETE")
        config = _config_from_run(run, workdir)
        plan = build_plan(config, run["run_id"], ledger.get_diffs(run["run_id"]), args.kind)
        ledger.save_plan(plan, utc_now())
        write_json(Path(args.out), plan)
        print(pretty_json({"plan_id": plan["plan_id"], "kind": plan["kind"], "operation_count": plan["operation_count"],
                           "manual_review_count": plan["manual_review_count"], "out": str(Path(args.out).resolve())}), end="")
        return 0


def cmd_apply(args: argparse.Namespace) -> int:
    plan = load_plan(args.plan)
    config = load_config(plan["config_path"])
    _check_workdir(config, args.workdir)
    db = _db_path(args.workdir, args.db, config.ledger_path)
    with Ledger(db) as ledger:
        result = execute_plan(config, plan, execute=args.execute, ledger=ledger, workdir=args.workdir)
        print(pretty_json(result.as_dict()), end="")
        return 0 if result.status in {"completed", "dry_run"} else 2


def cmd_verify(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).resolve()
    db = _db_path(workdir, args.db)
    with Ledger(db) as ledger:
        batch, config = _batch_context(ledger, args.batch_id, workdir)
        report = verify_batch(config, batch, ledger, workdir)
        print(pretty_json(report), end="")
        return 0 if report["status"] == "verified" else 2


def cmd_rollback(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).resolve()
    db = _db_path(workdir, args.db)
    with Ledger(db) as ledger:
        batch, config = _batch_context(ledger, args.batch_id, workdir)
        result = rollback_batch(config, batch, execute=args.execute, ledger=ledger, workdir=workdir)
        print(pretty_json(result), end="")
        return 0


def cmd_report(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).resolve()
    db = _db_path(workdir, args.db)
    with Ledger(db) as ledger:
        content = render_report(build_report(ledger, args.run_id), args.format)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(pretty_json({"out": str(path.resolve()), "format": args.format}), end="")
    else:
        print(content, end="" if content.endswith("\n") else "\n")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    serve_forever(args.workdir, args.host, args.port, args.db)
    return 0


def cmd_provenance_evidence(args: argparse.Namespace) -> int:
    print(pretty_json(inspect_report_origins(args.manifest)), end="")
    return 0


def cmd_provenance_adjudicate(args: argparse.Namespace) -> int:
    decision = adjudicate_report_release(args.manifest, contract_kinds=args.contract or None)
    if args.out_dir:
        decision["written"] = write_release_artifacts(args.manifest, decision, args.out_dir)
    print(pretty_json(decision), end="")
    return 0 if all(item["status"] == "allowed" for item in decision["publication_decisions"]) else 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m lakemirror", description="Local dual-replica lake consistency console")
    sub = parser.add_subparsers(dest="command", required=True)
    fixtures = sub.add_parser("fixtures", help="generate deterministic local fixture trees")
    fixtures.add_argument("--scenario", required=True, choices=["clean_pair", "drift_basic", "schema_drift", "snapshot_lag"])
    fixtures.add_argument("--seed", type=int, default=42)
    fixtures.add_argument("--workdir", required=True)
    fixtures.set_defaults(func=cmd_fixtures)

    scan = sub.add_parser("scan", help="reconcile primary and replica manifests")
    scan.add_argument("--config", required=True)
    scan.add_argument("--workdir", required=True)
    scan.add_argument("--db")
    scan.add_argument("--report")
    scan.set_defaults(func=cmd_scan)

    plan = sub.add_parser("plan", help="build a deterministic repair plan")
    plan.add_argument("--scan-id", default="latest")
    plan.add_argument("--workdir", required=True)
    plan.add_argument("--db")
    plan.add_argument("--out", required=True)
    plan.add_argument("--kind", choices=["executable", "dry-run"], default="executable")
    plan.set_defaults(func=cmd_plan)

    apply = sub.add_parser("apply", help="dry-run or execute a plan inside the replica sandbox")
    apply.add_argument("--plan", required=True)
    apply.add_argument("--workdir", required=True)
    apply.add_argument("--db")
    apply.add_argument("--execute", action="store_true")
    apply.set_defaults(func=cmd_apply)

    verify = sub.add_parser("verify", help="rescan and verify a completed execution batch")
    verify.add_argument("--batch-id", default="latest")
    verify.add_argument("--workdir", required=True)
    verify.add_argument("--db")
    verify.set_defaults(func=cmd_verify)

    rollback = sub.add_parser("rollback", help="restore a batch from its local backups")
    rollback.add_argument("--batch-id", default="latest")
    rollback.add_argument("--workdir", required=True)
    rollback.add_argument("--db")
    rollback.add_argument("--execute", action="store_true")
    rollback.set_defaults(func=cmd_rollback)

    report = sub.add_parser("report", help="render JSON, Markdown, or HTML")
    report.add_argument("--workdir", required=True)
    report.add_argument("--db")
    report.add_argument("--run-id", default="latest")
    report.add_argument("--format", choices=["json", "md", "html"], default="md")
    report.add_argument("--out")
    report.set_defaults(func=cmd_report)

    serve = sub.add_parser("serve", help="start the loopback-only read-only review console")
    serve.add_argument("--workdir", required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--db")
    serve.set_defaults(func=cmd_serve)

    origin = sub.add_parser("provenance-evidence", help="verify synthetic report origin evidence")
    origin.add_argument("--manifest", required=True)
    origin.set_defaults(func=cmd_provenance_evidence)

    release = sub.add_parser("provenance-adjudicate", help="decide report publication per contract")
    release.add_argument("--manifest", required=True)
    release.add_argument("--contract", action="append", choices=["reproducible_authorized", "origin_safe"])
    release.add_argument("--out-dir", help="write allowed local instances and receipts")
    release.set_defaults(func=cmd_provenance_adjudicate)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except LakeMirrorError as exc:
        return _fail(str(exc), exc.code)
    except OSError as exc:
        return _fail(str(exc), "OS_ERROR")
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
