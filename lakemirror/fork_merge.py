"""Three-way row merge for authorized local snapshot forks.

Two pre-authorized contracts decide whether an authoritative dual-parent
merge snapshot may be published:

* ``file_strict``: a logical file (partition + path) may change on at most one
  side relative to the common ancestor. Both sides changing the same logical
  file blocks publication even when their physical file names differ.
* ``field_disjoint``: rows are matched by a stable business key; a field may
  change on at most one side. Disjoint field changes are combined, while
  delete-versus-update, same-field conflicts, duplicate keys, missing files,
  header drift, and unknown ancestors block publication.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Any

from .manifest import FileEntry, Manifest

CONTRACTS = ("file_strict", "field_disjoint")


@dataclass(frozen=True)
class Block:
    code: str
    message: str
    key: str = ""
    column: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "key": self.key, "column": self.column}


@dataclass
class FieldProvenance:
    key: str
    column: str
    value: str
    source: str
    ancestor_value: str
    primary_value: str
    replica_value: str

    def as_dict(self) -> dict[str, str]:
        return {
            "key": self.key,
            "column": self.column,
            "value": self.value,
            "source": self.source,
            "ancestor_value": self.ancestor_value,
            "primary_value": self.primary_value,
            "replica_value": self.replica_value,
        }


@dataclass
class MergeOutcome:
    contract: str
    allowed: bool
    blocks: list[Block] = field(default_factory=list)
    rows: list[dict[str, str]] = field(default_factory=list)
    provenance: list[FieldProvenance] = field(default_factory=list)
    changed_files: dict[str, list[str]] = field(default_factory=dict)
    headers: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "allowed": self.allowed,
            "blocks": [item.as_dict() for item in self.blocks],
            "rows": self.rows,
            "provenance": [item.as_dict() for item in self.provenance],
            "changed_files": self.changed_files,
            "fingerprint_basis": self.fingerprint_basis(),
        }

    def fingerprint_basis(self) -> list[Any]:
        return [
            self.contract,
            self.allowed,
            [block.as_dict() for block in self.blocks],
            self.headers,
            self.rows,
            [[item.key, item.column, item.source] for item in self.provenance],
        ]


def read_csv_rows(content: bytes) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(content.decode("utf-8")))
    headers = list(reader.fieldnames or [])
    return headers, [dict(row) for row in reader]


def render_csv(headers: list[str], rows: list[dict[str, str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=headers, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({name: row.get(name, "") for name in headers})
    return buffer.getvalue().encode("utf-8")


def _partition_map(manifest: Manifest) -> dict[str, list[FileEntry]]:
    grouped: dict[str, list[FileEntry]] = {}
    for entry in manifest.files:
        grouped.setdefault(entry.partition, []).append(entry)
    return grouped


def changed_partitions(ancestor: Manifest, primary: Manifest, replica: Manifest) -> dict[str, list[str]]:
    base = {partition: sorted(entry.sha256 for entry in entries)
            for partition, entries in _partition_map(ancestor).items()}
    result: dict[str, list[str]] = {"primary": [], "replica": []}
    for side, manifest in (("primary", primary), ("replica", replica)):
        for partition, entries in _partition_map(manifest).items():
            digest = sorted(entry.sha256 for entry in entries)
            if partition not in base or base[partition] != digest:
                result[side].append(partition)
    return result


def changed_physical_files(ancestor: Manifest, primary: Manifest, replica: Manifest) -> dict[str, list[str]]:
    change = changed_partitions(ancestor, primary, replica)
    grouped = {"primary": _partition_map(primary), "replica": _partition_map(replica)}
    return {
        side: sorted(entry.path for partition in keys for entry in grouped[side][partition])
        for side, keys in change.items()
    }


def _file_strict_blocks(change: dict[str, list[str]]) -> list[Block]:
    return [
        Block("FILE_STRICT_BOTH_SIDES_CHANGED", f"partition changed on both branches: {partition}", key=partition)
        for partition in sorted(set(change["primary"]) & set(change["replica"]))
    ]


def _keyed(headers: list[str], rows: list[dict[str, str]], key: str, where: str) -> tuple[dict[str, dict[str, str]], list[Block]]:
    if key not in headers:
        return {}, [Block("MERGE_KEY_MISSING", f"{where} CSV lacks business key column {key}", column=key)]
    seen: set[str] = set()
    keyed: dict[str, dict[str, str]] = {}
    blocks: list[Block] = []
    for row in rows:
        value = row.get(key, "")
        if value in seen:
            blocks.append(Block("MERGE_DUPLICATE_KEY", f"{where} CSV contains duplicate business key {value}", key=value, column=key))
        seen.add(value)
        keyed[value] = row
    return keyed, blocks


def _field_merge(
    key: str,
    headers: list[str],
    base_rows: dict[str, dict[str, str]],
    primary_rows: dict[str, dict[str, str]],
    replica_rows: dict[str, dict[str, str]],
) -> tuple[list[dict[str, str]], list[FieldProvenance], list[Block]]:
    blocks: list[Block] = []
    provenance: list[FieldProvenance] = []
    columns = [name for name in headers if name != key]
    merged: dict[str, dict[str, str]] = {}
    for ident in sorted(set(base_rows) | set(primary_rows) | set(replica_rows)):
        base_row, left_row, right_row = base_rows.get(ident), primary_rows.get(ident), replica_rows.get(ident)
        if left_row is None and right_row is None:
            merged[ident] = dict(base_row)  # type: ignore[arg-type]
            continue
        if left_row is None or right_row is None:
            side = "primary" if left_row is not None else "replica"
            if base_row is not None:
                blocks.append(Block("MERGE_DELETE_VS_UPDATE", f"row {ident} is deleted on one branch but retained on {side}", key=ident))
                continue
            blocks.append(Block("MERGE_ROW_WITHOUT_ANCESTOR", f"row {ident} exists only on {side}", key=ident))
            continue
        if base_row is None:
            blocks.append(Block("MERGE_ROW_WITHOUT_ANCESTOR", f"row {ident} has no common ancestor", key=ident))
            continue
        merged_row: dict[str, str] = {key: ident}
        for column in columns:
            base_value = base_row.get(column, "")
            left_value = left_row.get(column, "")
            right_value = right_row.get(column, "")
            left_changed, right_changed = left_value != base_value, right_value != base_value
            if left_changed and right_changed and left_value != right_value:
                blocks.append(Block("MERGE_FIELD_CONFLICT", f"row {ident} column {column} changed to different values", key=ident, column=column))
                continue
            if left_changed and right_changed:
                value, source = left_value, "both"
            elif left_changed:
                value, source = left_value, "primary"
            elif right_changed:
                value, source = right_value, "replica"
            else:
                value, source = base_value, "ancestor"
            merged_row[column] = value
            provenance.append(FieldProvenance(ident, column, value, source, base_value, left_value, right_value))
        merged[ident] = merged_row
    return [merged[ident] for ident in sorted(merged)], provenance, blocks


def evaluate_contract(
    contract: str,
    key: str,
    ancestor: Manifest,
    primary: Manifest,
    replica: Manifest,
    csv_bytes: dict[str, bytes],
) -> MergeOutcome:
    if contract not in CONTRACTS:
        raise ValueError(f"unknown merge contract: {contract}")
    partition_change = changed_partitions(ancestor, primary, replica)
    physical_change = changed_physical_files(ancestor, primary, replica)
    if contract == "file_strict":
        blocks = _file_strict_blocks(partition_change)
        return MergeOutcome(contract, not blocks, blocks, changed_files=physical_change)
    blocks: list[Block] = []
    grouped = {
        "ancestor": _partition_map(ancestor),
        "primary": _partition_map(primary),
        "replica": _partition_map(replica),
    }
    headers: list[str] = []
    merged_rows: list[dict[str, str]] = []
    provenance: list[FieldProvenance] = []
    partitions = sorted(set().union(*(set(mapping) for mapping in grouped.values())))
    for partition in partitions:
        entries = {role: grouped[role].get(partition) for role in grouped}
        if any(value is None for value in entries.values()):
            missing = [role for role, value in entries.items() if value is None]
            blocks.append(Block("MERGE_FILE_PRESENCE_MISMATCH", f"partition {partition} absent on {','.join(missing)}", key=partition))
            continue
        if any(len(value) != 1 for value in entries.values()):  # type: ignore[union-attr]
            blocks.append(Block("MERGE_PARTITION_LAYOUT_UNSUPPORTED", f"partition {partition} must hold exactly one CSV per snapshot", key=partition))
            continue
        paths = {role: entries[role][0].path for role in entries}  # type: ignore[index]
        parsed = {role: read_csv_rows(csv_bytes[f"{role}:{paths[role]}"]) for role in grouped}
        role_headers = {role: value[0] for role, value in parsed.items()}
        if len(set(map(tuple, role_headers.values()))) != 1:
            blocks.append(Block("MERGE_SCHEMA_MISMATCH", f"CSV headers differ in partition {partition}", key=partition))
            continue
        headers = role_headers["ancestor"]
        keyed_rows: dict[str, dict[str, dict[str, str]]] = {}
        fatal = False
        for role in grouped:
            rows, row_blocks = _keyed(headers, parsed[role][1], key, f"{role}:{partition}")
            blocks.extend(row_blocks)
            keyed_rows[role] = rows
            fatal = fatal or bool(row_blocks)
        if fatal:
            continue
        rows, item_provenance, item_blocks = _field_merge(
            key, headers, keyed_rows["ancestor"], keyed_rows["primary"], keyed_rows["replica"]
        )
        blocks.extend(item_blocks)
        if not item_blocks:
            merged_rows, provenance = rows, item_provenance
    allowed = not blocks
    return MergeOutcome(
        contract,
        allowed,
        blocks,
        merged_rows if allowed else [],
        provenance if allowed else [],
        physical_change,
        headers,
    )


def merged_file_entry(template: FileEntry, path: str, content: bytes, row_count: int) -> FileEntry:
    from .util import sha256_bytes

    return FileEntry(path, template.partition, len(content), row_count, sha256_bytes(content))
