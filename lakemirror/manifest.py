"""Parsing and structural validation of local lake table manifests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .util import LakeMirrorError, is_valid_sha256, normalize_relpath

_ALLOWED_TOP = {
    "table",
    "snapshot_id",
    "parent_snapshot_id",
    "sequence",
    "created_at",
    "schema",
    "partition_spec",
    "files",
    "stats",
}


@dataclass(frozen=True)
class ManifestIssue:
    code: str
    message: str
    source: str
    json_path: str
    field_name: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "source": self.source,
            "json_path": self.json_path,
            "field": self.field_name,
        }

    def __str__(self) -> str:
        return f"{self.source}:{self.json_path}:{self.code}: {self.message}"


class ManifestError(LakeMirrorError):
    def __init__(self, issues: list[ManifestIssue] | ManifestIssue) -> None:
        values = issues if isinstance(issues, list) else [issues]
        super().__init__("; ".join(str(item) for item in values), "MANIFEST_INVALID")
        self.issues = tuple(values)


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    nullable: bool

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type, "nullable": self.nullable}


@dataclass(frozen=True)
class PartitionSpec:
    columns: tuple[str, ...]

    def as_dict(self) -> list[str]:
        return list(self.columns)


@dataclass(frozen=True)
class FileEntry:
    path: str
    partition: str
    size: int
    row_count: int
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "partition": self.partition,
            "size": self.size,
            "row_count": self.row_count,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class Manifest:
    table: str
    snapshot_id: str
    parent_snapshot_id: str | None
    sequence: int
    created_at: str
    fields: tuple[Field, ...]
    partition_spec: PartitionSpec
    files: tuple[FileEntry, ...]
    stats: dict[str, Any] = field(default_factory=dict)
    source_path: str = "<memory>"
    issues: tuple[ManifestIssue, ...] = ()

    def file_map(self) -> dict[tuple[str, str], FileEntry]:
        return {(item.partition, item.path): item for item in self.files}

    def partitions(self) -> set[str]:
        return {item.partition for item in self.files}

    def total_bytes(self) -> int:
        return sum(item.size for item in self.files)

    def node(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "snapshot_id": self.snapshot_id,
            "parent_snapshot_id": self.parent_snapshot_id,
            "sequence": self.sequence,
            "created_at": self.created_at,
            "file_count": len(self.files),
            "total_bytes": self.total_bytes(),
        }


def _issue(
    source: str, json_path: str, field_name: str, code: str, message: str
) -> ManifestIssue:
    return ManifestIssue(code, message, source, json_path, field_name)


def _parse_partition(value: Any, spec: tuple[str, ...], path: str) -> tuple[str | None, str]:
    if not isinstance(value, str):
        return None, "partition must be a string"
    if not spec:
        return (value, "") if value == "" else (None, "unpartitioned table must use empty partition")
    if not value:
        return None, "partition value is empty"
    pairs: dict[str, str] = {}
    for item in value.split(","):
        if "=" not in item:
            return None, f"partition component lacks '=': {item!r}"
        key, raw = item.split("=", 1)
        if not key or key in pairs:
            return None, f"partition key is empty or duplicated: {key!r}"
        if raw == "":
            return None, f"partition value is empty for {key}"
        pairs[key] = raw
    if set(pairs) != set(spec):
        return None, f"partition columns {sorted(pairs)} do not match spec {list(spec)}"
    ordered = ",".join(f"{key}={pairs[key]}" for key in spec)
    return ordered, ""


def _schema(raw: Any, source: str) -> tuple[Field, ...]:
    if not isinstance(raw, list) or not raw:
        raise ManifestError(_issue(source, "$.schema", "schema", "MANIFEST_FIELD_TYPE", "schema must be a non-empty array"))
    fields: list[Field] = []
    names: set[str] = set()
    for index, item in enumerate(raw):
        where = f"$.schema[{index}]"
        if not isinstance(item, dict):
            raise ManifestError(_issue(source, where, "schema", "MANIFEST_FIELD_TYPE", "field must be an object"))
        if set(item) - {"name", "type", "nullable"}:
            raise ManifestError(_issue(source, where, "schema", "MANIFEST_UNKNOWN_FIELD", "unknown schema field key"))
        name, kind, nullable = item.get("name"), item.get("type"), item.get("nullable")
        if not isinstance(name, str) or not name.strip():
            raise ManifestError(_issue(source, where, "name", "MANIFEST_FIELD_TYPE", "name must be a string"))
        if not isinstance(kind, str) or not kind.strip():
            raise ManifestError(_issue(source, where, "type", "MANIFEST_FIELD_TYPE", "type must be a string"))
        if not isinstance(nullable, bool):
            raise ManifestError(_issue(source, where, "nullable", "MANIFEST_FIELD_TYPE", "nullable must be boolean"))
        if name in names:
            raise ManifestError(_issue(source, where, "name", "DUPLICATE_SCHEMA_FIELD", f"duplicate schema field: {name}"))
        names.add(name)
        fields.append(Field(name=name.strip(), type=kind.strip().upper(), nullable=nullable))
    return tuple(fields)


def _partition_spec(raw: Any, source: str) -> PartitionSpec:
    if not isinstance(raw, list) or not all(isinstance(item, str) and item for item in raw):
        raise ManifestError(_issue(source, "$.partition_spec", "partition_spec", "MANIFEST_FIELD_TYPE", "partition_spec must be an array of strings"))
    if len(set(raw)) != len(raw):
        raise ManifestError(_issue(source, "$.partition_spec", "partition_spec", "MANIFEST_FIELD_TYPE", "partition_spec contains duplicates"))
    return PartitionSpec(tuple(raw))


def _files(
    raw: Any, source: str, spec: tuple[str, ...]
) -> tuple[tuple[FileEntry, ...], tuple[ManifestIssue, ...]]:
    if not isinstance(raw, list):
        raise ManifestError(_issue(source, "$.files", "files", "MANIFEST_FIELD_TYPE", "files must be an array"))
    files: list[FileEntry] = []
    issues: list[ManifestIssue] = []
    seen: dict[str, str] = {}
    for index, item in enumerate(raw):
        where = f"$.files[{index}]"
        if not isinstance(item, dict):
            raise ManifestError(_issue(source, where, "files", "MANIFEST_FIELD_TYPE", "file entry must be an object"))
        if set(item) - {"path", "partition", "size", "row_count", "sha256"}:
            raise ManifestError(_issue(source, where, "files", "MANIFEST_UNKNOWN_FIELD", "unknown file field"))
        raw_path = item.get("path")
        if not isinstance(raw_path, str):
            raise ManifestError(_issue(source, where, "path", "MANIFEST_FIELD_TYPE", "path must be a string"))
        try:
            path = normalize_relpath(raw_path)
        except LakeMirrorError as exc:
            issues.append(_issue(source, where, "path", "PATH_OUTSIDE_ROOT", str(exc)))
            continue
        size, rows, digest = item.get("size"), item.get("row_count"), item.get("sha256")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ManifestError(_issue(source, where, "size", "MANIFEST_FIELD_TYPE", "size must be a non-negative integer"))
        if not isinstance(rows, int) or isinstance(rows, bool) or rows < 0:
            raise ManifestError(_issue(source, where, "row_count", "MANIFEST_FIELD_TYPE", "row_count must be a non-negative integer"))
        if not isinstance(digest, str) or not is_valid_sha256(digest):
            raise ManifestError(_issue(source, where, "sha256", "MANIFEST_FIELD_TYPE", "sha256 must be 64 hex characters"))
        partition, error = _parse_partition(item.get("partition"), spec, path)
        if partition is None:
            issues.append(_issue(source, where, "partition", "INVALID_PARTITION_VALUE", error))
            continue
        if path in seen or path.casefold() in seen:
            issues.append(_issue(source, where, "path", "DUPLICATE_LOGICAL_PATH", f"duplicate logical path: {path}"))
            continue
        seen[path] = path
        seen[path.casefold()] = path
        files.append(FileEntry(path, partition, size, rows, digest.lower()))
    return tuple(files), tuple(issues)


def parse_manifest(data: Any, source: str = "<memory>", strict: bool = False) -> Manifest:
    if not isinstance(data, dict):
        raise ManifestError(_issue(source, "$", "manifest", "MANIFEST_FIELD_TYPE", "manifest must be an object"))
    unknown = sorted(set(data) - _ALLOWED_TOP)
    if unknown:
        raise ManifestError(_issue(source, "$", "manifest", "MANIFEST_UNKNOWN_FIELD", f"unknown field(s): {', '.join(unknown)}"))
    required = _ALLOWED_TOP - {"stats"}
    missing = sorted(required - set(data))
    if missing:
        raise ManifestError(_issue(source, "$", missing[0], "MANIFEST_FIELD_MISSING", f"missing field(s): {', '.join(missing)}"))
    table = data["table"]
    snapshot = data["snapshot_id"]
    parent = data["parent_snapshot_id"]
    sequence = data["sequence"]
    created = data["created_at"]
    if not isinstance(table, str) or not table.strip():
        raise ManifestError(_issue(source, "$.table", "table", "MANIFEST_FIELD_TYPE", "table must be a non-empty string"))
    if not isinstance(snapshot, str) or not snapshot.strip():
        raise ManifestError(_issue(source, "$.snapshot_id", "snapshot_id", "MANIFEST_FIELD_TYPE", "snapshot_id must be a non-empty string"))
    if parent is not None and (not isinstance(parent, str) or not parent.strip()):
        raise ManifestError(_issue(source, "$.parent_snapshot_id", "parent_snapshot_id", "MANIFEST_FIELD_TYPE", "parent must be null or a non-empty string"))
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise ManifestError(_issue(source, "$.sequence", "sequence", "MANIFEST_FIELD_TYPE", "sequence must be a positive integer"))
    if not isinstance(created, str) or not created.strip():
        raise ManifestError(_issue(source, "$.created_at", "created_at", "MANIFEST_FIELD_TYPE", "created_at must be a string"))
    stats = data.get("stats", {})
    if not isinstance(stats, dict):
        raise ManifestError(_issue(source, "$.stats", "stats", "MANIFEST_FIELD_TYPE", "stats must be an object"))
    fields = _schema(data["schema"], source)
    spec = _partition_spec(data["partition_spec"], source)
    file_entries, issues = _files(data["files"], source, spec.columns)
    if strict and issues:
        raise ManifestError(list(issues))
    return Manifest(
        table=table.strip(),
        snapshot_id=snapshot.strip(),
        parent_snapshot_id=parent.strip() if isinstance(parent, str) else None,
        sequence=sequence,
        created_at=created.strip(),
        fields=fields,
        partition_spec=spec,
        files=file_entries,
        stats=stats,
        source_path=source,
        issues=issues,
    )


def load_manifest(path: str | Path, strict: bool = False) -> Manifest:
    source = str(path)
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(_issue(source, "$", "manifest", "MANIFEST_MISSING", "manifest file does not exist")) from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(_issue(source, f"line {exc.lineno}, column {exc.colno}", "manifest", "MANIFEST_INVALID_JSON", exc.msg)) from exc
    return parse_manifest(raw, source=source, strict=strict)
