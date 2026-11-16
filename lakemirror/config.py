"""Declarative configuration loading and validation for Lake Mirror."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .config_types import (
    Checks,
    Config,
    ConfigError,
    IgnoreRule,
    RepairPolicy,
    TableConfig,
)
from .util import CONFLICT_CODES, LakeMirrorError, fingerprint, normalize_relpath, within

_TABLE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")


def _require_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError("must be an object", field)
    return value


def _check_unknown(value: dict[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigError(f"unknown field(s): {', '.join(unknown)}", field)


def _required_str(value: dict[str, Any], key: str, field: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ConfigError(f"{key} must be a non-empty string", f"{field}.{key}")
    return item.strip()


def _required_bool(value: dict[str, Any], key: str, field: str) -> bool:
    item = value.get(key)
    if not isinstance(item, bool):
        raise ConfigError(f"{key} must be a boolean", f"{field}.{key}")
    return item


def _string_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError("must be an array of strings", field)
    return tuple(value)


def _resolve(base: Path, raw: str, field: str) -> Path:
    path = Path(raw)
    return (base / path).resolve(strict=False) if not path.is_absolute() else path.resolve(strict=False)


def _load_tables(raw: Any, primary: Path, replica: Path) -> dict[str, TableConfig]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError("tables must be a non-empty array", "tables")
    tables: dict[str, TableConfig] = {}
    seen_paths: set[str] = set()
    for index, item in enumerate(raw):
        field = f"tables[{index}]"
        entry = _require_mapping(item, field)
        _check_unknown(entry, {"name", "path", "business_key", "partition_columns"}, field)
        name = _required_str(entry, "name", field)
        if not _TABLE_NAME.fullmatch(name):
            raise ConfigError("table name has unsupported characters", f"{field}.name")
        if name in tables:
            raise ConfigError(f"duplicate table name: {name}", f"{field}.name")
        path_raw = _required_str(entry, "path", field)
        try:
            path = normalize_relpath(path_raw)
        except LakeMirrorError as exc:
            raise ConfigError(str(exc), f"{field}.path") from exc
        if path in seen_paths:
            raise ConfigError(f"duplicate table path: {path}", f"{field}.path")
        seen_paths.add(path)
        part_columns = _string_list(entry.get("partition_columns", []), f"{field}.partition_columns")
        if len(set(part_columns)) != len(part_columns):
            raise ConfigError("partition_columns contains duplicates", f"{field}.partition_columns")
        primary_table = primary.joinpath(*path.split("/"))
        replica_table = replica.joinpath(*path.split("/"))
        if not primary_table.is_dir():
            raise ConfigError(f"primary table path does not exist: {path}", f"{field}.path")
        if not replica_table.is_dir():
            raise ConfigError(f"replica table path does not exist: {path}", f"{field}.path")
        tables[name] = TableConfig(
            name=name,
            path=path,
            business_key=_required_str(entry, "business_key", field),
            partition_columns=part_columns,
        )
    return tables


def _load_checks(raw: Any) -> Checks:
    entry = _require_mapping(raw, "checks")
    allowed = {
        "file_size",
        "sha256",
        "row_count",
        "partition_coverage",
        "snapshot_chain",
        "schema_compat",
        "partition_spec",
    }
    _check_unknown(entry, allowed, "checks")
    values = {key: _required_bool(entry, key, "checks") for key in allowed if key in entry}
    return Checks(**values)


def _known_codes(raw: Any, field: str) -> frozenset[str]:
    values = _string_list(raw, field)
    unknown = sorted(set(values) - CONFLICT_CODES)
    if unknown:
        raise ConfigError(f"unknown conflict category: {', '.join(unknown)}", field)
    return frozenset(values)


def _load_repair(raw: Any, workdir: Path) -> tuple[RepairPolicy, bool]:
    entry = _require_mapping(raw, "repair")
    _check_unknown(entry, {"mode", "auto_fix", "manual_review", "safety"}, "repair")
    mode = entry.get("mode", "lenient")
    if mode not in {"lenient", "strict"}:
        raise ConfigError("mode must be lenient or strict", "repair.mode")
    auto = _known_codes(entry.get("auto_fix", []), "repair.auto_fix")
    manual = _known_codes(entry.get("manual_review", []), "repair.manual_review")
    overlap = sorted(auto & manual)
    if overlap:
        raise ConfigError(f"categories cannot be both auto and manual: {', '.join(overlap)}", "repair")
    safety = _require_mapping(entry.get("safety"), "repair.safety")
    _check_unknown(
        safety, {"allow_delete", "max_operations", "backup_dir", "dry_run_default"}, "repair.safety"
    )
    allow_delete = _required_bool(safety, "allow_delete", "repair.safety")
    max_operations = safety.get("max_operations")
    if not isinstance(max_operations, int) or isinstance(max_operations, bool) or not 1 <= max_operations <= 10000:
        raise ConfigError("max_operations must be an integer in [1, 10000]", "repair.safety.max_operations")
    backup_raw = safety.get("backup_dir", "backups")
    if not isinstance(backup_raw, str) or not backup_raw.strip():
        raise ConfigError("backup_dir must be a non-empty string", "repair.safety.backup_dir")
    backup = _resolve(workdir, backup_raw.strip(), "repair.safety.backup_dir")
    if not within(workdir, backup):
        raise ConfigError("backup_dir must stay inside workdir", "repair.safety.backup_dir")
    dry_default = _required_bool(safety, "dry_run_default", "repair.safety")
    if not dry_default:
        raise ConfigError("dry_run_default must be true; --execute is the only opt-in", "repair.safety.dry_run_default")
    return RepairPolicy(mode, auto, manual, allow_delete, max_operations, backup), dry_default


def _load_ignore_rules(raw: Any, table_names: set[str]) -> tuple[IgnoreRule, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("ignore_rules must be an array", "ignore_rules")
    rules: list[IgnoreRule] = []
    for index, item in enumerate(raw):
        field = f"ignore_rules[{index}]"
        entry = _require_mapping(item, field)
        _check_unknown(entry, {"code", "table", "path_prefix", "reason"}, field)
        code = _required_str(entry, "code", field)
        if code != "*" and code not in CONFLICT_CODES:
            raise ConfigError(f"unknown conflict category: {code}", f"{field}.code")
        table = entry.get("table", "*")
        if not isinstance(table, str) or (table != "*" and table not in table_names):
            raise ConfigError("table must be '*' or a configured table", f"{field}.table")
        prefix = entry.get("path_prefix", "")
        if not isinstance(prefix, str):
            raise ConfigError("path_prefix must be a string", f"{field}.path_prefix")
        if prefix:
            try:
                prefix = normalize_relpath(prefix)
            except LakeMirrorError as exc:
                raise ConfigError(str(exc), f"{field}.path_prefix") from exc
        rules.append(IgnoreRule(code, table, prefix, _required_str(entry, "reason", field)))
    return tuple(rules)


def load_config(path: str | Path) -> Config:
    config_path = Path(path).resolve()
    if not config_path.is_file():
        raise ConfigError(f"configuration file not found: {config_path}", "path")
    try:
        import json

        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ConfigError(f"cannot parse JSON: {exc}", "path") from exc
    data = _require_mapping(raw, "root")
    _check_unknown(
        data,
        {
            "schema_version",
            "primary_root",
            "replica_root",
            "workdir",
            "ledger",
            "tables",
            "checks",
            "repair",
            "ignore_rules",
        },
        "root",
    )
    version = data.get("schema_version", 1)
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        raise ConfigError("schema_version must be the integer 1", "schema_version")
    base = config_path.parent
    primary = _resolve(base, _required_str(data, "primary_root", "root"), "primary_root")
    replica = _resolve(base, _required_str(data, "replica_root", "root"), "replica_root")
    workdir = _resolve(base, _required_str(data, "workdir", "root"), "workdir")
    ledger_raw = data.get("ledger", "ledger.db")
    if not isinstance(ledger_raw, str) or not ledger_raw.strip():
        raise ConfigError("ledger must be a non-empty string", "ledger")
    ledger = _resolve(base, ledger_raw.strip(), "ledger")
    for label, folder in (("primary_root", primary), ("replica_root", replica), ("workdir", workdir)):
        if not folder.is_dir():
            raise ConfigError(f"{label} is not an existing directory: {folder}", label)
        if folder.is_symlink():
            raise ConfigError(f"{label} must not be a symbolic link", label)
    if primary == replica or primary.is_relative_to(replica) or replica.is_relative_to(primary):
        raise ConfigError("primary_root and replica_root must be separate trees", "roots")
    if not within(workdir, replica):
        raise ConfigError("replica_root must stay inside workdir", "replica_root")
    if not within(workdir, ledger):
        raise ConfigError("ledger must stay inside workdir", "ledger")
    if ledger.exists() and ledger.is_symlink():
        raise ConfigError("ledger must not be a symbolic link", "ledger")
    tables = _load_tables(data.get("tables"), primary, replica)
    checks = _load_checks(data.get("checks"))
    repair, dry_default = _load_repair(data.get("repair"), workdir)
    rules = _load_ignore_rules(data.get("ignore_rules"), set(tables))
    normalized = {
        "primary_root": str(primary),
        "replica_root": str(replica),
        "workdir": str(workdir),
        "ledger": str(ledger),
        "tables": [
            {
                "name": table.name,
                "path": table.path,
                "business_key": table.business_key,
                "partition_columns": list(table.partition_columns),
            }
            for table in tables.values()
        ],
        "checks": vars(checks),
        "repair": {
            "mode": repair.mode,
            "auto_fix": sorted(repair.auto_fix),
            "manual_review": sorted(repair.manual_review),
            "allow_delete": repair.allow_delete,
            "max_operations": repair.max_operations,
            "backup_dir": str(repair.backup_dir),
        },
        "ignore_rules": [vars(rule) for rule in rules],
    }
    return Config(
        config_path=config_path,
        primary_root=primary,
        replica_root=replica,
        workdir=workdir,
        ledger_path=ledger,
        tables=tables,
        checks=checks,
        repair=repair,
        ignore_rules=rules,
        dry_run_default=dry_default,
        fingerprint=fingerprint(normalized),
    )
