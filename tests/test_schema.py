"""Schema and partition-spec drift diagnosis tests."""

from __future__ import annotations

import unittest

from lakemirror.schema import compare_schemas, field_signature, schema_blocked
from tests.support import manifest

BASE = [
    {"name": "id", "type": "LONG", "nullable": False},
    {"name": "amount", "type": "DECIMAL", "nullable": True},
    {"name": "dt", "type": "STRING", "nullable": False},
]


def codes(primary_schema: list[dict], replica_schema: list[dict],
          primary_spec: list[str] | None = None, replica_spec: list[str] | None = None) -> list[str]:
    findings = compare_schemas(
        manifest(schema=primary_schema, spec=primary_spec or ["dt"]),
        manifest(schema=replica_schema, spec=replica_spec or ["dt"]),
    )
    return [item.code for item in findings]


class SchemaTests(unittest.TestCase):
    def test_identical_schema_has_no_findings(self) -> None:
        self.assertEqual(compare_schemas(manifest(schema=BASE), manifest(schema=BASE)), [])

    def test_missing_field_blocks(self) -> None:
        replica = [item for item in BASE if item["name"] != "amount"]
        findings = compare_schemas(manifest(schema=BASE), manifest(schema=replica))
        self.assertEqual([item.code for item in findings], ["SCHEMA_FIELD_MISSING"])
        self.assertTrue(findings[0].blocking)
        self.assertEqual(findings[0].field, "amount")
        self.assertIsNone(findings[0].actual)

    def test_type_and_nullability_drift(self) -> None:
        replica = [dict(BASE[0]), {"name": "amount", "type": "STRING", "nullable": False}, dict(BASE[2])]
        self.assertEqual(codes(BASE, replica), ["SCHEMA_TYPE_MISMATCH", "SCHEMA_NULLABILITY_MISMATCH"])

    def test_nullable_replica_only_field_is_additive(self) -> None:
        replica = BASE + [{"name": "note", "type": "STRING", "nullable": True}]
        findings = compare_schemas(manifest(schema=BASE), manifest(schema=replica))
        self.assertEqual([item.code for item in findings], ["SCHEMA_ADDITIVE_OK"])
        self.assertFalse(findings[0].blocking)
        self.assertFalse(schema_blocked(findings))

    def test_non_nullable_replica_only_field_blocks(self) -> None:
        replica = BASE + [{"name": "note", "type": "STRING", "nullable": False}]
        findings = compare_schemas(manifest(schema=BASE), manifest(schema=replica))
        self.assertIn("SCHEMA_TYPE_MISMATCH", [item.code for item in findings])
        self.assertTrue(schema_blocked(findings))

    def test_shared_field_order_mismatch(self) -> None:
        replica = [BASE[1], BASE[0], BASE[2]]
        findings = compare_schemas(manifest(schema=BASE), manifest(schema=replica))
        self.assertEqual([item.code for item in findings], ["SCHEMA_FIELD_ORDER_MISMATCH"])

    def test_partition_spec_mismatch_and_missing_column(self) -> None:
        findings = compare_schemas(
            manifest(schema=BASE, spec=["dt"]),
            manifest(schema=BASE, spec=["dt", "shard"]),
        )
        found = [item.code for item in findings]
        self.assertIn("PARTITION_SPEC_MISMATCH", found)
        self.assertIn("PARTITION_COLUMN_MISSING", found)
        self.assertTrue(all(item.blocking for item in findings))

    def test_partition_column_missing_from_primary_schema(self) -> None:
        findings = compare_schemas(
            manifest(schema=BASE, spec=["dt", "ghost"]),
            manifest(schema=BASE, spec=["dt", "ghost"]),
        )
        self.assertEqual([item.code for item in findings], ["PARTITION_COLUMN_MISSING", "PARTITION_COLUMN_MISSING"])
        self.assertEqual({item.field for item in findings}, {"ghost"})

    def test_field_signature_is_plain_dicts(self) -> None:
        value = field_signature(manifest(schema=BASE).fields)
        self.assertEqual(value[0], {"name": "id", "type": "LONG", "nullable": False})
        self.assertEqual(len(value), 3)


if __name__ == "__main__":
    unittest.main()
