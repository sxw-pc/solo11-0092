"""File, partition, schema, and snapshot reconciliation for local lake replicas."""

from __future__ import annotations

from dataclasses import replace
import time
from pathlib import Path
from typing import Any, Iterable

from .config import Config, IgnoreRule
from .fork_reconcile import reconcile_fork_head
from .manifest import FileEntry, Manifest, ManifestIssue, load_manifest
from .schema import SchemaFinding, compare_schemas
from .lag import LagFinding, analyze_chains
from .snapshot import SnapshotChain, load_chain
from .reconcile_types import Diff, ScanResult
from .util import SEVERITY, canonical_json, fingerprint


_SUGGESTIONS = {
    "FILE_MISSING_IN_REPLICA": "Copy the verified primary file into the replica.",
    "FILE_EXTRA_IN_REPLICA": "Delete the unreferenced replica file after backup.",
    "FILE_SIZE_MISMATCH": "Replace the replica file with the primary version.",
    "FILE_SHA256_MISMATCH": "Replace the file only when policy permits checksum repair.",
    "ROW_COUNT_MISMATCH": "Replace the file or request manual data review under strict mode.",
    "PARTITION_MISSING": "Replicate every file in the missing partition.",
    "PARTITION_EXTRA": "Review and remove the extra replica partition files.",
    "DUPLICATE_LOGICAL_PATH": "Resolve duplicate metadata manually; never auto-delete.",
    "PATH_OUTSIDE_ROOT": "Block automated execution and inspect the manifest.",
    "EMPTY_TABLE": "Inspect whether replication failed or the table is intentionally empty.",
    "INVALID_PARTITION_VALUE": "Fix the manifest partition value manually.",
    "SNAPSHOT_LAG": "Replicate missing snapshots through the reviewed workflow.",
    "SNAPSHOT_CHAIN_BROKEN": "Repair snapshot history manually; no automatic metadata copy.",
    "SNAPSHOT_FORK": "Resolve divergent snapshot lineage manually.",
    "POINTER_STALE": "Point the replica at its latest verified snapshot.",
    "SCHEMA_ADDITIVE_OK": "No repair required; document the compatible nullable addition.",
    "SCHEMA_TYPE_MISMATCH": "Schema drift is blocking and needs owner review.",
    "SCHEMA_FIELD_MISSING": "Restore the missing field through a reviewed schema process.",
    "SCHEMA_FIELD_ORDER_MISMATCH": "Review physical field order; no automatic rewrite.",
    "SCHEMA_NULLABILITY_MISMATCH": "Review nullability with the table owner.",
    "PARTITION_SPEC_MISMATCH": "Review the partition spec; no automatic rewrite.",
    "PARTITION_COLUMN_MISSING": "Add the partition field through a reviewed schema process.",
}


def _match_ignore(rule: IgnoreRule, code: str, table: str, path: str) -> bool:
    if rule.code not in {"*", code} or rule.table not in {"*", table}:
        return False
    if rule.path_prefix and not path.startswith(rule.path_prefix):
        return False
    return True


def _raw_diff(config: Config, code: str, table: str, partition: str, path: str,
              expected: Any, actual: Any, evidence: dict[str, Any] | None = None) -> Diff:
    return Diff(
        diff_id="",
        code=code,
        table=table,
        partition=partition,
        path=path,
        expected=expected,
        actual=actual,
        severity=SEVERITY[code],
        suggestion=_SUGGESTIONS[code],
        auto_fixable=config.can_auto(code),
        evidence=evidence or {},
    )


def _apply_ignores(config: Config, diffs: list[Diff]) -> list[Diff]:
    result: list[Diff] = []
    seen: dict[str, int] = {}
    for item in sorted(diffs, key=lambda diff: (diff.code, diff.table, diff.partition, diff.path, canonical_json(diff.expected), canonical_json(diff.actual))):
        rule = next((value for value in config.ignore_rules if _match_ignore(value, item.code, item.table, item.path)), None)
        base = fingerprint([item.code, item.table, item.partition, item.path, item.expected, item.actual])
        occurrence = seen.get(base, 0) + 1
        seen[base] = occurrence
        if rule:
            item = replace(item, ignored=True, ignore_reason=rule.reason, auto_fixable=False)
        result.append(replace(item, diff_id=f"diff-{base[:12]}-{occurrence}"))
    return result


def _manifest_issue_diffs(config: Config, table: str, side: str, issues: Iterable[ManifestIssue]) -> list[Diff]:
    output = []
    for issue in issues:
        if issue.code not in SEVERITY:
            continue
        output.append(
            _raw_diff(
                config,
                issue.code,
                table,
                "",
                issue.json_path,
                {"side": side, "expected": "valid manifest path/value"},
                {"side": side, "message": issue.message},
                {"source": issue.source, "json_path": issue.json_path, "field": issue.field_name},
            )
        )
    return output


def _file_diff(config: Config, table: str, code: str, key: tuple[str, str], expected: Any, actual: Any) -> Diff:
    partition, path = key
    return _raw_diff(config, code, table, partition, path, expected, actual, {"logical_key": f"{partition}|{path}"})


def _entry_dict(entry: FileEntry | None) -> Any:
    return None if entry is None else entry.as_dict()


def _file_findings(config: Config, table: str, primary: Manifest, replica: Manifest) -> list[Diff]:
    diffs: list[Diff] = []
    left, right = primary.file_map(), replica.file_map()
    if not replica.files:
        diffs.append(
            _raw_diff(
                config,
                "EMPTY_TABLE",
                table,
                "",
                "",
                {"file_count": len(primary.files)},
                {"file_count": 0},
                {"manifest": replica.source_path},
            )
        )
    if config.checks.partition_coverage:
        primary_parts, replica_parts = primary.partitions(), replica.partitions()
        for partition in sorted(primary_parts - replica_parts):
            expected = [item for item in primary.files if item.partition == partition]
            diffs.append(
                _raw_diff(
                    config,
                    "PARTITION_MISSING",
                    table,
                    partition,
                    "",
                    {"file_count": len(expected), "total_bytes": sum(item.size for item in expected)},
                    None,
                    {"partition": partition},
                )
            )
        for partition in sorted(replica_parts - primary_parts):
            actual = [item for item in replica.files if item.partition == partition]
            diffs.append(
                _raw_diff(
                    config,
                    "PARTITION_EXTRA",
                    table,
                    partition,
                    "",
                    None,
                    {"file_count": len(actual), "total_bytes": sum(item.size for item in actual)},
                    {"partition": partition},
                )
            )
    for key in sorted(left.keys() - right.keys()):
        diffs.append(_file_diff(config, table, "FILE_MISSING_IN_REPLICA", key, _entry_dict(left[key]), None))
    for key in sorted(right.keys() - left.keys()):
        diffs.append(_file_diff(config, table, "FILE_EXTRA_IN_REPLICA", key, None, _entry_dict(right[key])))
    for key in sorted(left.keys() & right.keys()):
        expected, actual = left[key], right[key]
        if config.checks.file_size and expected.size != actual.size:
            diffs.append(
                _file_diff(config, table, "FILE_SIZE_MISMATCH", key, expected.size, actual.size)
            )
        if config.checks.sha256 and expected.sha256 != actual.sha256:
            diffs.append(
                _file_diff(config, table, "FILE_SHA256_MISMATCH", key, expected.sha256, actual.sha256)
            )
        if config.checks.row_count and expected.row_count != actual.row_count:
            diffs.append(
                _file_diff(config, table, "ROW_COUNT_MISMATCH", key, expected.row_count, actual.row_count)
            )
    return diffs


def _schema_findings(config: Config, table: str, findings: list[SchemaFinding]) -> list[Diff]:
    output = []
    for finding in findings:
        if finding.code.startswith("PARTITION_") and not config.checks.partition_spec:
            continue
        if not finding.code.startswith("PARTITION_") and not config.checks.schema_compat:
            continue
        output.append(
            _raw_diff(
                config,
                finding.code,
                table,
                "",
                finding.field,
                finding.expected,
                finding.actual,
                {"message": finding.message, "blocking": finding.blocking},
            )
        )
    return output


def _chain_findings(config: Config, table: str, findings: Iterable[LagFinding]) -> list[Diff]:
    output = []
    for finding in findings:
        output.append(
            _raw_diff(
                config, finding.code, table, "", "", finding.expected, finding.actual, finding.evidence
            )
        )
    return output


def _load_pair(config: Config, table_name: str) -> tuple[Manifest, Manifest]:
    table = config.table(table_name)
    primary_path = Path(config.primary_root).joinpath(*table.path.split("/")) / "_manifest.json"
    replica_path = Path(config.replica_root).joinpath(*table.path.split("/")) / "_manifest.json"
    primary = load_manifest(primary_path)
    replica = load_manifest(replica_path)
    for manifest, side in ((primary, "primary"), (replica, "replica")):
        if manifest.table != table.name:
            raise ValueError(f"{side} manifest table {manifest.table!r} does not match config {table.name!r}")
    return primary, replica


def scan_config(config: Config, scan_id: str | None = None) -> ScanResult:
    scan_id = scan_id or f"scan-{time.time_ns()}"
    all_diffs: list[Diff] = []
    chains: list[SnapshotChain] = []
    stats: dict[str, Any] = {"counts_by_code": {}, "counts_by_severity": {}, "ignored_count": 0}
    table_stats: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for name, table in config.tables.items():
        primary, replica = _load_pair(config, name)
        basic_issues = _manifest_issue_diffs(config, name, "primary", primary.issues) + _manifest_issue_diffs(config, name, "replica", replica.issues)
        all_diffs.extend(basic_issues)
        if tuple(table.partition_columns) != primary.partition_spec.columns:
            all_diffs.append(
                _raw_diff(config, "PARTITION_SPEC_MISMATCH", name, "", "partition_spec",
                          list(table.partition_columns), list(primary.partition_spec.columns),
                          {"source": "config_vs_primary_manifest"})
            )
        if config.checks.snapshot_chain:
            primary_chain = load_chain(Path(config.primary_root).joinpath(*table.path.split("/")), name, "primary")
            replica_chain = load_chain(Path(config.replica_root).joinpath(*table.path.split("/")), name, "replica")
            chains.extend([primary_chain, replica_chain])
            all_diffs.extend(_chain_findings(config, name, analyze_chains(primary_chain, replica_chain)))
        if config.checks.schema_compat or config.checks.partition_spec:
            all_diffs.extend(_schema_findings(config, name, compare_schemas(primary, replica)))
        all_diffs.extend(_file_findings(config, name, primary, replica))
        table_stats[name] = {
            "primary_files": len(primary.files),
            "replica_files": len(replica.files),
            "primary_bytes": primary.total_bytes(),
            "replica_bytes": replica.total_bytes(),
            "primary_snapshot": primary.snapshot_id,
            "replica_snapshot": replica.snapshot_id,
            "business_key": table.business_key,
            "partition_columns": list(table.partition_columns),
        }
    diffs = tuple(_apply_ignores(config, all_diffs))
    for item in diffs:
        stats["counts_by_code"][item.code] = stats["counts_by_code"].get(item.code, 0) + 1
        stats["counts_by_severity"][item.severity] = stats["counts_by_severity"].get(item.severity, 0) + 1
        stats["ignored_count"] += int(item.ignored)
    stats["counts_by_code"] = dict(sorted(stats["counts_by_code"].items()))
    stats["counts_by_severity"] = dict(sorted(stats["counts_by_severity"].items()))
    stats.update(
        {
            "total_diffs": len(diffs),
            "active_diffs": sum(1 for item in diffs if not item.ignored),
            "auto_fixable_diffs": sum(1 for item in diffs if item.auto_fixable),
            "manual_review_diffs": sum(1 for item in diffs if not item.auto_fixable and not item.ignored),
            "tables": len(config.tables),
            "ignore_rules": [vars(rule) for rule in config.ignore_rules],
        }
    )
    return ScanResult(scan_id, config, diffs, tuple(chains), table_stats, stats, tuple(warnings))


def reconcile_fork_table(table_dir: str | Path, table: str) -> dict[str, Any]:
    """Public reconcile entry: readable/blocked verdict for a fork merge head."""

    return reconcile_fork_head(table_dir, table)
