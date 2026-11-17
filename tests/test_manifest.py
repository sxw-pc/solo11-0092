"""Manifest parsing, normalization, and structural error tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lakemirror.manifest import ManifestError, load_manifest, parse_manifest
from tests.support import file_entry, manifest_doc


def _code(exc: ManifestError) -> str:
    return exc.issues[0].code


class ManifestTests(unittest.TestCase):
    def test_valid_manifest_is_normalized(self) -> None:
        value = parse_manifest(manifest_doc(files=[file_entry("data/dt=2024-01-01/a.csv")]), "demo.json")
        self.assertEqual(value.table, "probe")
        self.assertEqual(value.partition_spec.columns, ("dt",))
        self.assertEqual(value.fields[0].type, "LONG")
        self.assertEqual(value.files[0].path, "data/dt=2024-01-01/a.csv")
        self.assertEqual(value.file_map()[( "dt=2024-01-01", "data/dt=2024-01-01/a.csv")].row_count, 1)
        self.assertEqual(value.total_bytes(), 1)

    def test_missing_manifest_file_is_locatable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.json"
            with self.assertRaises(ManifestError) as caught:
                load_manifest(path)
            issue = caught.exception.issues[0]
            self.assertEqual(issue.code, "MANIFEST_MISSING")
            self.assertEqual(issue.source, str(path))
            self.assertIn("does not exist", issue.message)

    def test_invalid_json_reports_line_and_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text('{"table": "probe",\n  "snapshot_id": }', encoding="utf-8")
            with self.assertRaises(ManifestError) as caught:
                load_manifest(path)
            issue = caught.exception.issues[0]
            self.assertEqual(issue.code, "MANIFEST_INVALID_JSON")
            self.assertRegex(issue.json_path, r"^line \d+, column \d+$")

    def test_missing_required_field(self) -> None:
        doc = manifest_doc()
        del doc["table"]
        with self.assertRaises(ManifestError) as caught:
            parse_manifest(doc, "demo.json")
        self.assertEqual(_code(caught.exception), "MANIFEST_FIELD_MISSING")
        self.assertEqual(caught.exception.issues[0].field_name, "table")

    def test_unknown_fields_are_rejected(self) -> None:
        doc = manifest_doc()
        doc["mystery"] = True
        with self.assertRaises(ManifestError) as caught:
            parse_manifest(doc, "demo.json")
        self.assertEqual(_code(caught.exception), "MANIFEST_UNKNOWN_FIELD")
        self.assertIn("mystery", caught.exception.issues[0].message)

    def test_duplicate_schema_field(self) -> None:
        doc = manifest_doc()
        doc["schema"] = list(doc["schema"]) + [dict(doc["schema"][0])]
        with self.assertRaises(ManifestError) as caught:
            parse_manifest(doc, "demo.json")
        self.assertEqual(_code(caught.exception), "DUPLICATE_SCHEMA_FIELD")
        self.assertEqual(caught.exception.issues[0].json_path, "$.schema[2]")

    def test_path_escape_is_semantic_unless_strict(self) -> None:
        doc = manifest_doc(files=[file_entry("../outside.csv", "dt=2024-01-01")])
        manifest = parse_manifest(doc, "demo.json")
        self.assertEqual(manifest.files, ())
        self.assertEqual(manifest.issues[0].code, "PATH_OUTSIDE_ROOT")
        self.assertEqual(manifest.issues[0].json_path, "$.files[0]")
        with self.assertRaises(ManifestError) as caught:
            parse_manifest(doc, "demo.json", strict=True)
        self.assertEqual(_code(caught.exception), "PATH_OUTSIDE_ROOT")

    def test_duplicate_logical_paths_and_case_collisions(self) -> None:
        first = file_entry("data/dt=2024-01-01/a.csv")
        second = file_entry("data/dt=2024-01-01/A.csv")
        manifest = parse_manifest(manifest_doc(files=[first, second]), "demo.json")
        self.assertEqual(len(manifest.files), 1)
        self.assertEqual(manifest.issues[0].code, "DUPLICATE_LOGICAL_PATH")
        self.assertIn("duplicate logical path", manifest.issues[0].message)

    def test_partition_values_must_match_spec(self) -> None:
        cases = [
            ("dt=2024-01-01,extra=1", "do not match spec"),
            ("dt=", "partition value is empty"),
            ("dt", "lacks '='"),
            ("dt=2024-01-01,dt=2024-01-02", "duplicated"),
        ]
        for partition, message in cases:
            with self.subTest(partition=partition):
                doc = manifest_doc(files=[file_entry(partition=partition)])
                manifest = parse_manifest(doc, "demo.json")
                self.assertEqual(manifest.issues[0].code, "INVALID_PARTITION_VALUE")
                self.assertIn(message, manifest.issues[0].message)

    def test_partition_is_canonicalized_to_spec_order(self) -> None:
        doc = manifest_doc(spec=["day", "shard"], files=[file_entry(partition="shard=0,day=2024-02-01")])
        manifest = parse_manifest(doc, "demo.json")
        self.assertEqual(manifest.files[0].partition, "day=2024-02-01,shard=0")

    def test_invalid_sha_and_boolean_size_rejected(self) -> None:
        doc = manifest_doc(files=[file_entry(sha="not-a-sha")])
        with self.assertRaises(ManifestError) as caught:
            parse_manifest(doc, "demo.json")
        self.assertEqual(_code(caught.exception), "MANIFEST_FIELD_TYPE")
        doc = manifest_doc(files=[file_entry()])
        doc["files"][0]["size"] = True
        with self.assertRaises(ManifestError):
            parse_manifest(doc, "demo.json")

    def test_load_manifest_reads_utf8_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_text(json.dumps(manifest_doc()), encoding="utf-8")
            manifest = load_manifest(path)
            self.assertEqual(manifest.snapshot_id, "snap-1")
            self.assertEqual(manifest.source_path, str(path))


if __name__ == "__main__":
    unittest.main()
