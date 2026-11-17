"""Schema and partition-spec drift classification (diagnosis only, never DDL)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .manifest import Field, Manifest

_ADDITIVE = "SCHEMA_ADDITIVE_OK"


@dataclass(frozen=True)
class SchemaFinding:
    code: str
    field: str
    expected: Any
    actual: Any
    message: str
    blocking: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "field": self.field,
            "expected": self.expected,
            "actual": self.actual,
            "message": self.message,
            "blocking": self.blocking,
        }


def _finding(code: str, field: str, expected: Any, actual: Any, message: str, blocking: bool = True) -> SchemaFinding:
    return SchemaFinding(code, field, expected, actual, message, blocking)


def compare_schemas(primary: Manifest, replica: Manifest) -> list[SchemaFinding]:
    """Compare fields and partition specs. No manifest is modified here."""

    findings: list[SchemaFinding] = []
    primary_fields = list(primary.fields)
    replica_fields = list(replica.fields)
    primary_map = {item.name: item for item in primary_fields}
    replica_map = {item.name: item for item in replica_fields}

    for name, expected in primary_map.items():
        actual = replica_map.get(name)
        if actual is None:
            findings.append(
                _finding(
                    "SCHEMA_FIELD_MISSING",
                    name,
                    expected.as_dict(),
                    None,
                    f"replica is missing field {name}",
                )
            )
            continue
        if expected.type != actual.type:
            findings.append(
                _finding(
                    "SCHEMA_TYPE_MISMATCH",
                    name,
                    expected.type,
                    actual.type,
                    f"field {name} type differs",
                )
            )
        if expected.nullable != actual.nullable:
            findings.append(
                _finding(
                    "SCHEMA_NULLABILITY_MISMATCH",
                    name,
                    expected.nullable,
                    actual.nullable,
                    f"field {name} nullability differs",
                )
            )

    extra = [item for item in replica_fields if item.name not in primary_map]
    if extra and all(item.nullable for item in extra):
        findings.append(
            _finding(
                _ADDITIVE,
                ",".join(item.name for item in extra),
                [],
                [item.as_dict() for item in extra],
                "replica adds nullable field(s); compatible for read-only consumers",
                blocking=False,
            )
        )
    for item in extra:
        if not item.nullable:
            findings.append(
                _finding(
                    "SCHEMA_TYPE_MISMATCH",
                    item.name,
                    "field absent or nullable",
                    item.as_dict(),
                    f"replica-only field {item.name} is non-nullable",
                )
            )

    shared_primary = [item.name for item in primary_fields if item.name in replica_map]
    shared_replica = [item.name for item in replica_fields if item.name in primary_map]
    if shared_primary != shared_replica:
        findings.append(
            _finding(
                "SCHEMA_FIELD_ORDER_MISMATCH",
                "schema",
                shared_primary,
                shared_replica,
                "shared fields have a different physical order",
            )
        )

    expected_spec = list(primary.partition_spec.columns)
    actual_spec = list(replica.partition_spec.columns)
    if expected_spec != actual_spec:
        findings.append(
            _finding(
                "PARTITION_SPEC_MISMATCH",
                "partition_spec",
                expected_spec,
                actual_spec,
                "partition columns or ordering differ",
            )
        )
    for side, manifest, spec in (
        ("replica", replica, actual_spec),
        ("primary", primary, expected_spec),
    ):
        names = {field.name for field in manifest.fields}
        for column in spec:
            if column not in names:
                findings.append(
                    _finding(
                        "PARTITION_COLUMN_MISSING",
                        column,
                        "a declared schema field",
                        None,
                        f"{side} partition column {column} is absent from its schema",
                    )
                )
    return findings


def schema_blocked(findings: list[SchemaFinding]) -> bool:
    return any(item.blocking for item in findings)


def field_signature(fields: tuple[Field, ...]) -> list[dict[str, Any]]:
    return [item.as_dict() for item in fields]
