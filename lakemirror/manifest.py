"""Parsing, structural validation, and canonical serialization of local manifests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .util import LakeMirrorError, canonical_json, is_valid_sha256, sha256_text

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
    "merge",
}
_MERGE_CONTRACTS = frozenset({"file_strict", "field_disjoint"})


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
class MergeRef:
    """One parent of a dual-parent merge commit with byte-level file evidence."""

    role: str
    snapshot_id: str
    manifest_sha256: str
    files: tuple[FileEntry, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "snapshot_id": self.snapshot_id,
            "manifest_sha256": self.manifest_sha256,
            "files": [item.as_dict() for item in self.files],
        }


@dataclass(frozen=True)
class MergeLineage:
    """Dual-parent merge evidence; keeps both branches plus their common ancestor."""

    contract: str
    ancestor: MergeRef
    parents: tuple[MergeRef, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "ancestor": self.ancestor.as_dict(),
            "parents": [item.as_dict() for item in self.parents],
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
    merge: MergeLineage | None = None

    def file_map(self) -> dict[tuple[str, str], FileEntry]:
        return {(item.partition, item.path): item for item in self.files}

    def partitions(self) -> set[str]:
        return {item.partition for item in self.files}

    def total_bytes(self) -> int:
        return sum(item.size for item in self.files)

    def is_merge(self) -> bool:
        return self.merge is not None

    def parent_ids(self) -> tuple[str, ...]:
        if self.merge is not None:
            return tuple(item.snapshot_id for item in self.merge.parents)
        return (self.parent_snapshot_id,) if self.parent_snapshot_id is not None else ()

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


def _issue(source: str, json_path: str, field_name: str, code: str, message: str) -> ManifestIssue:
    return ManifestIssue(code, message, source, json_path, field_name)


def _merge_ref(raw: Any, source: str, where: str, role: str, spec: tuple[str, ...]) -> MergeRef:
    if not isinstance(raw, dict) or set(raw) != {"role", "snapshot_id", "manifest_sha256", "files"}:
        raise ManifestError(_issue(source, where, "merge", "MANIFEST_FIELD_TYPE", "merge ref must be an object with role, snapshot_id, manifest_sha256, files"))
    if raw["role"] != role or not isinstance(raw["snapshot_id"], str) or not raw["snapshot_id"].strip():
        raise ManifestError(_issue(source, where, "merge", "MANIFEST_FIELD_TYPE", f"merge ref {role} has invalid identity"))
    if not isinstance(raw["manifest_sha256"], str) or not is_valid_sha256(raw["manifest_sha256"]):
        raise ManifestError(_issue(source, where, "manifest_sha256", "MANIFEST_FIELD_TYPE", "merge ref manifest_sha256 must be 64 hex characters"))
    entries, ref_issues = _parse_files(raw["files"], source, spec, f"{where}.files")
    if ref_issues:
        raise ManifestError(list(ref_issues))
    return MergeRef(role, raw["snapshot_id"].strip(), raw["manifest_sha256"].lower(), entries)


def _merge_lineage(raw: Any, source: str, parent: str | None, spec: tuple[str, ...]) -> MergeLineage:
    where = "$.merge"
    if not isinstance(raw, dict) or set(raw) != {"contract", "ancestor", "parents"}:
        raise ManifestError(_issue(source, where, "merge", "MANIFEST_FIELD_TYPE", "merge must contain contract, ancestor, parents"))
    contract = raw["contract"]
    if not isinstance(contract, str) or contract not in _MERGE_CONTRACTS:
        raise ManifestError(_issue(source, f"{where}.contract", "contract", "MANIFEST_FIELD_TYPE", "merge contract must be file_strict or field_disjoint"))
    ancestor = _merge_ref(raw["ancestor"], source, f"{where}.ancestor", "ancestor", spec)
    parents_raw = raw["parents"]
    if not isinstance(parents_raw, list) or len(parents_raw) != 2:
        raise ManifestError(_issue(source, f"{where}.parents", "parents", "MANIFEST_FIELD_TYPE", "merge parents must contain exactly two refs"))
    roles = [item.get("role") if isinstance(item, dict) else None for item in parents_raw]
    if roles != ["primary", "replica"]:
        raise ManifestError(_issue(source, f"{where}.parents", "parents", "MANIFEST_FIELD_TYPE", "merge parent roles must be primary then replica"))
    parents = tuple(
        _merge_ref(item, source, f"{where}.parents[{index}]", role, spec)
        for index, (item, role) in enumerate(zip(parents_raw, roles))
    )
    ids = [item.snapshot_id for item in parents]
    if len(set(ids)) != 2 or ancestor.snapshot_id in ids:
        raise ManifestError(_issue(source, f"{where}.parents", "parents", "MANIFEST_FIELD_TYPE", "merge parents must be two distinct snapshots different from ancestor"))
    if parent is None or parent != parents[0].snapshot_id:
        raise ManifestError(_issue(source, "$.parent_snapshot_id", "parent_snapshot_id", "MANIFEST_FIELD_TYPE", "merge commit parent_snapshot_id must equal the first merge parent"))
    return MergeLineage(contract, ancestor, parents)


def parse_manifest(data: Any, source: str = "<memory>", strict: bool = False) -> Manifest:
    if not isinstance(data, dict):
        raise ManifestError(_issue(source, "$", "manifest", "MANIFEST_FIELD_TYPE", "manifest must be an object"))
    unknown = sorted(set(data) - _ALLOWED_TOP)
    if unknown:
        raise ManifestError(_issue(source, "$", "manifest", "MANIFEST_UNKNOWN_FIELD", f"unknown field(s): {', '.join(unknown)}"))
    required = _ALLOWED_TOP - {"stats", "merge"}
    missing = sorted(required - set(data))
    if missing:
        raise ManifestError(_issue(source, "$", missing[0], "MANIFEST_FIELD_MISSING", f"missing field(s): {', '.join(missing)}"))
    table, snapshot, parent, sequence, created = (data["table"], data["snapshot_id"], data["parent_snapshot_id"], data["sequence"], data["created_at"])
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
    parent_value = parent.strip() if isinstance(parent, str) else None
    fields = _parse_schema(data["schema"], source)
    spec = _parse_partition_spec(data["partition_spec"], source)
    merge = _merge_lineage(data["merge"], source, parent_value, spec.columns) if "merge" in data else None
    file_entries, issues = _parse_files(data["files"], source, spec.columns)
    if strict and issues:
        raise ManifestError(list(issues))
    return Manifest(
        table=table.strip(),
        snapshot_id=snapshot.strip(),
        parent_snapshot_id=parent_value,
        sequence=sequence,
        created_at=created.strip(),
        fields=fields,
        partition_spec=spec,
        files=file_entries,
        stats=stats,
        source_path=source,
        issues=issues,
        merge=merge,
    )


def canonical_manifest_doc(manifest: Manifest) -> dict[str, Any]:
    """Return the byte-stable document shape used for manifests and digests."""

    doc: dict[str, Any] = {
        "table": manifest.table,
        "snapshot_id": manifest.snapshot_id,
        "parent_snapshot_id": manifest.parent_snapshot_id,
        "sequence": manifest.sequence,
        "created_at": manifest.created_at,
        "schema": [item.as_dict() for item in manifest.fields],
        "partition_spec": manifest.partition_spec.as_dict(),
        "files": [item.as_dict() for item in manifest.files],
        "stats": manifest.stats,
    }
    if manifest.merge is not None:
        doc["merge"] = manifest.merge.as_dict()
    return doc


def manifest_canonical_text(manifest: Manifest) -> str:
    return json.dumps(canonical_manifest_doc(manifest), sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def manifest_digest(manifest: Manifest) -> str:
    return sha256_text(canonical_json(canonical_manifest_doc(manifest)))


def load_manifest(path: str | Path, strict: bool = False) -> Manifest:
    source = str(path)
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(_issue(source, "$", "manifest", "MANIFEST_MISSING", "manifest file does not exist")) from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(_issue(source, f"line {exc.lineno}, column {exc.colno}", "manifest", "MANIFEST_INVALID_JSON", exc.msg)) from exc
    return parse_manifest(raw, source=source, strict=strict)


# Imported late to keep the dataclasses above available during module import.
from .manifest_schema import parse_files as _parse_files  # noqa: E402
from .manifest_schema import parse_partition_spec as _parse_partition_spec  # noqa: E402
from .manifest_schema import parse_schema as _parse_schema  # noqa: E402
