"""End-to-end file, partition, schema, and snapshot reconciliation tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lakemirror.reconcile import scan_config
from tests.support import make_config


def table_dir(config, name: str, side: str) -> Path:
    table = config.table(name)
    root = config.primary_root if side == "primary" else config.replica_root
    return Path(root).joinpath(*table.path.split("/"))


def rewrite_manifest(config, name: str, mutate) -> None:
    path = table_dir(config, name, "replica") / "_manifest.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    mutate(doc)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class ReconcileTests(unittest.TestCase):
    def test_clean_pair_has_no_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = scan_config(make_config(tmp, scenario="clean_pair"), "scan-clean")
            self.assertEqual(result.diffs, ())
            self.assertEqual(result.stats["active_diffs"], 0)
            self.assertEqual(result.stats["tables"], 2)
            self.assertEqual(result.table_stats["orders"]["primary_files"], 3)

    def test_drift_basic_detects_all_file_and_partition_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = scan_config(make_config(tmp), "scan-drift")
            found = {item.code for item in result.diffs}
            expected = {
                "FILE_MISSING_IN_REPLICA", "FILE_EXTRA_IN_REPLICA", "FILE_SIZE_MISMATCH",
                "FILE_SHA256_MISMATCH", "ROW_COUNT_MISMATCH", "PARTITION_MISSING",
                "PARTITION_EXTRA", "POINTER_STALE",
            }
            self.assertEqual(found, expected)
            self.assertEqual(result.stats["active_diffs"], 14)
            self.assertTrue(all(item.auto_fixable for item in result.diffs))
            self.assertIn("FILE_MISSING_IN_REPLICA", result.stats["counts_by_code"])
            self.assertEqual(sum(result.stats["counts_by_code"].values()), 14)

    def test_strict_mode_blocks_checksum_and_row_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = scan_config(make_config(tmp, mode="strict"), "scan-strict")
            blocked = [item for item in result.diffs if item.code in {"FILE_SHA256_MISMATCH", "ROW_COUNT_MISMATCH"}]
            self.assertEqual(len(blocked), 3)
            self.assertTrue(all(not item.auto_fixable for item in blocked))
            self.assertEqual(result.stats["auto_fixable_diffs"], 11)
            self.assertEqual(result.stats["manual_review_diffs"], 3)

    def test_ignore_rules_are_audited_and_disable_auto(self) -> None:
        rule = {"code": "FILE_EXTRA_IN_REPLICA", "table": "events",
                "path_prefix": "data/day=2024-02-09", "reason": "pending source correction"}
        with tempfile.TemporaryDirectory() as tmp:
            result = scan_config(make_config(tmp, ignore_rules=[rule]), "scan-ignore")
            ignored = [item for item in result.diffs if item.ignored]
            self.assertEqual(len(ignored), 1)
            self.assertEqual(ignored[0].ignore_reason, "pending source correction")
            self.assertFalse(ignored[0].auto_fixable)
            self.assertEqual(result.stats["ignored_count"], 1)
            self.assertEqual(result.stats["active_diffs"], 13)
            self.assertEqual(result.stats["ignore_rules"][0]["reason"], "pending source correction")

    def test_disabled_row_count_check_removes_that_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = scan_config(make_config(tmp, checks={"row_count": False}), "scan-no-rows")
            self.assertNotIn("ROW_COUNT_MISMATCH", {item.code for item in result.diffs})

    def test_schema_drift_is_manual_and_never_ddl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = scan_config(make_config(tmp, scenario="schema_drift"), "scan-schema")
            found = {item.code for item in result.diffs}
            for code in ("SCHEMA_TYPE_MISMATCH", "SCHEMA_FIELD_MISSING", "SCHEMA_NULLABILITY_MISMATCH",
                         "SCHEMA_ADDITIVE_OK", "PARTITION_SPEC_MISMATCH", "PARTITION_COLUMN_MISSING"):
                self.assertIn(code, found)
            additive = next(item for item in result.diffs if item.code == "SCHEMA_ADDITIVE_OK")
            self.assertFalse(additive.auto_fixable)
            self.assertTrue(any(item.code == "SCHEMA_TYPE_MISMATCH" and not item.auto_fixable for item in result.diffs))

    def test_snapshot_lag_and_stale_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = scan_config(make_config(tmp, scenario="snapshot_lag"), "scan-lag")
            found = {item.code for item in result.diffs}
            self.assertEqual(found, {"SNAPSHOT_LAG", "POINTER_STALE"})
            lag = next(item for item in result.diffs if item.code == "SNAPSHOT_LAG")
            self.assertEqual(lag.severity, "high")
            self.assertIn("sequence_delta", lag.evidence)
            self.assertFalse(lag.auto_fixable)

    def test_manifest_path_escape_and_duplicate_path_become_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, scenario="clean_pair")
            def mutate(doc: dict) -> None:
                template = dict(doc["files"][0])
                escaped = dict(template, path="../outside.csv")
                duplicate = dict(template)
                doc["files"] = doc["files"] + [escaped, duplicate]
            rewrite_manifest(config, "orders", mutate)
            result = scan_config(config, "scan-manifest")
            found = {item.code for item in result.diffs}
            self.assertIn("PATH_OUTSIDE_ROOT", found)
            self.assertIn("DUPLICATE_LOGICAL_PATH", found)
            for code in ("PATH_OUTSIDE_ROOT", "DUPLICATE_LOGICAL_PATH"):
                diff = next(item for item in result.diffs if item.code == code)
                self.assertFalse(diff.auto_fixable)

    def test_empty_replica_table_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, scenario="clean_pair")
            rewrite_manifest(config, "orders", lambda doc: doc.update({"files": []}))
            result = scan_config(config, "scan-empty")
            empty = next(item for item in result.diffs if item.code == "EMPTY_TABLE")
            self.assertEqual(empty.table, "orders")
            self.assertFalse(empty.auto_fixable)

    def test_diff_ids_are_stable_for_same_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            first = scan_config(config, "scan-one")
            second = scan_config(config, "scan-two")
            ids = [[(item.diff_id, item.code, item.table, item.path) for item in result.diffs]
                   for result in (first, second)]
            self.assertEqual(ids[0], ids[1])

    def test_result_issue_evidence_is_json_serializable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = scan_config(make_config(tmp), "scan-evidence")
            payload = [item.as_dict() for item in result.diffs]
            text = json.dumps(payload, sort_keys=True)
            self.assertIn("logical_key", text)
            self.assertEqual(len(result.chains), 4)


if __name__ == "__main__":
    unittest.main()
