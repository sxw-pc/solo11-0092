"""Schema, partition-spec, and file-entry parsing for local manifests."""

from __future__ import annotations

from typing import Any

from .util import LakeMirrorError, normalize_relpath
from .manifest import Field, FileEntry, ManifestError, ManifestIssue, PartitionSpec


def issue(source: str, json_path: str, field_name: str, code: str, message: str) -> ManifestIssue:
    return ManifestIssue(code, message, source, json_path, field_name)


def parse_partition(value: Any, spec: tuple[str, ...], path: str) -> tuple[str | None, str]:
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


def parse_schema(raw: Any, source: str) -> tuple[Field, ...]:
    if not isinstance(raw, list) or not raw:
        raise ManifestError(issue(source, "$.schema", "schema", "MANIFEST_FIELD_TYPE", "schema must be a non-empty array"))
    fields: list[Field] = []
    names: set[str] = set()
    for index, item in enumerate(raw):
        where = f"$.schema[{index}]"
        if not isinstance(item, dict):
            raise ManifestError(issue(source, where, "schema", "MANIFEST_FIELD_TYPE", "field must be an object"))
        if set(item) - {"name", "type", "nullable"}:
            raise ManifestError(issue(source, where, "schema", "MANIFEST_UNKNOWN_FIELD", "unknown schema field key"))
        name, kind, nullable = item.get("name"), item.get("type"), item.get("nullable")
        if not isinstance(name, str) or not name.strip():
            raise ManifestError(issue(source, where, "name", "MANIFEST_FIELD_TYPE", "name must be a string"))
        if not isinstance(kind, str) or not kind.strip():
            raise ManifestError(issue(source, where, "type", "MANIFEST_FIELD_TYPE", "type must be a string"))
        if not isinstance(nullable, bool):
            raise ManifestError(issue(source, where, "nullable", "MANIFEST_FIELD_TYPE", "nullable must be boolean"))
        if name in names:
            raise ManifestError(issue(source, where, "name", "DUPLICATE_SCHEMA_FIELD", f"duplicate schema field: {name}"))
        names.add(name)
        fields.append(Field(name=name.strip(), type=kind.strip().upper(), nullable=nullable))
    return tuple(fields)


def parse_partition_spec(raw: Any, source: str) -> PartitionSpec:
    if not isinstance(raw, list) or not all(isinstance(item, str) and item for item in raw):
        raise ManifestError(issue(source, "$.partition_spec", "partition_spec", "MANIFEST_FIELD_TYPE", "partition_spec must be an array of strings"))
    if len(set(raw)) != len(raw):
        raise ManifestError(issue(source, "$.partition_spec", "partition_spec", "MANIFEST_FIELD_TYPE", "partition_spec contains duplicates"))
    return PartitionSpec(tuple(raw))


def parse_files(raw: Any, source: str, spec: tuple[str, ...], where: str = "$.files") -> tuple[tuple[FileEntry, ...], tuple[ManifestIssue, ...]]:
    if not isinstance(raw, list):
        raise ManifestError(issue(source, where, "files", "MANIFEST_FIELD_TYPE", "files must be an array"))
    files: list[FileEntry] = []
    issues: list[ManifestIssue] = []
    seen: dict[str, str] = {}
    from .util import is_valid_sha256

    for index, item in enumerate(raw):
        item_where = f"{where}[{index}]"
        if not isinstance(item, dict):
            raise ManifestError(issue(source, item_where, "files", "MANIFEST_FIELD_TYPE", "file entry must be an object"))
        if set(item) - {"path", "partition", "size", "row_count", "sha256"}:
            raise ManifestError(issue(source, item_where, "files", "MANIFEST_UNKNOWN_FIELD", "unknown file field"))
        raw_path = item.get("path")
        if not isinstance(raw_path, str):
            raise ManifestError(issue(source, item_where, "path", "MANIFEST_FIELD_TYPE", "path must be a string"))
        try:
            path = normalize_relpath(raw_path)
        except LakeMirrorError as exc:
            issues.append(issue(source, item_where, "path", "PATH_OUTSIDE_ROOT", str(exc)))
            continue
        size, rows, digest = item.get("size"), item.get("row_count"), item.get("sha256")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ManifestError(issue(source, item_where, "size", "MANIFEST_FIELD_TYPE", "size must be a non-negative integer"))
        if not isinstance(rows, int) or isinstance(rows, bool) or rows < 0:
            raise ManifestError(issue(source, item_where, "row_count", "MANIFEST_FIELD_TYPE", "row_count must be a non-negative integer"))
        if not isinstance(digest, str) or not is_valid_sha256(digest):
            raise ManifestError(issue(source, item_where, "sha256", "MANIFEST_FIELD_TYPE", "sha256 must be 64 hex characters"))
        partition, error = parse_partition(item.get("partition"), spec, path)
        if partition is None:
            issues.append(issue(source, item_where, "partition", "INVALID_PARTITION_VALUE", error))
            continue
        if path in seen or path.casefold() in seen:
            issues.append(issue(source, item_where, "path", "DUPLICATE_LOGICAL_PATH", f"duplicate logical path: {path}"))
            continue
        seen[path] = path
        seen[path.casefold()] = path
        files.append(FileEntry(path, partition, size, rows, digest.lower()))
    return tuple(files), tuple(issues)
