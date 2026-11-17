"""Snapshot-chain reconstruction and lag-analysis tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lakemirror.snapshot import (
    SnapshotError,
    analyze_chains,
    build_chain,
    load_chain,
    read_pointer,
)
from tests.support import manifest, manifest_doc


def chain(*manifests, table: str = "probe", side: str = "replica", pointer: str | None = None):
    return build_chain(manifests, table, side, pointer)


class SnapshotChainTests(unittest.TestCase):
    def test_linear_chain_is_clean(self) -> None:
        value = chain(
            manifest(snapshot_id="s1", sequence=1),
            manifest(snapshot_id="s2", parent="s1", sequence=2),
            manifest(snapshot_id="s3", parent="s2", sequence=3),
            pointer="s3",
        )
        self.assertEqual(value.ids, {"s1", "s2", "s3"})
        self.assertEqual(value.latest_id, "s3")
        self.assertEqual(value.issues, ())
        self.assertFalse(value.broken)
        self.assertFalse(value.forked)

    def test_duplicate_snapshot_id(self) -> None:
        value = chain(manifest(snapshot_id="s1", sequence=1), manifest(snapshot_id="s1", sequence=2))
        self.assertEqual([item.code for item in value.issues], ["DUPLICATE_SNAPSHOT_ID"])
        self.assertTrue(value.broken)

    def test_missing_parent_and_sequence_regression(self) -> None:
        value = chain(
            manifest(snapshot_id="s1", sequence=1),
            manifest(snapshot_id="s2", parent="ghost", sequence=5),
            manifest(snapshot_id="s3", parent="s1", sequence=1),
            pointer="s2",
        )
        found = {item.code for item in value.issues}
        self.assertIn("SNAPSHOT_PARENT_MISSING", found)
        self.assertIn("SNAPSHOT_SEQUENCE_REGRESSION", found)
        self.assertTrue(value.broken)

    def test_multiple_roots_and_duplicate_sequence(self) -> None:
        value = chain(
            manifest(snapshot_id="a", sequence=1),
            manifest(snapshot_id="b", sequence=1),
        )
        found = {item.code for item in value.issues}
        self.assertEqual(found, {"MULTIPLE_ROOTS", "DUPLICATE_SEQUENCE"})
        self.assertTrue(value.broken)
        self.assertTrue(value.forked)

    def test_fork_is_detected(self) -> None:
        value = chain(
            manifest(snapshot_id="s1", sequence=1),
            manifest(snapshot_id="s2", parent="s1", sequence=2),
            manifest(snapshot_id="s3", parent="s1", sequence=3),
            pointer="s3",
        )
        self.assertEqual([item.code for item in value.issues], ["SNAPSHOT_FORK"])
        self.assertTrue(value.forked)
        self.assertFalse(value.broken)

    def test_pointer_target_missing(self) -> None:
        value = chain(manifest(snapshot_id="s1", sequence=1), pointer="s9")
        self.assertEqual([item.code for item in value.issues], ["POINTER_TARGET_MISSING"])
        self.assertTrue(value.broken)

    def test_read_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(read_pointer(root))
            (root / "_pointer.json").write_text('{"snapshot_id": "s2"}', encoding="utf-8")
            self.assertEqual(read_pointer(root), "s2")
            (root / "_pointer.json").write_text("{broken", encoding="utf-8")
            with self.assertRaises(SnapshotError) as caught:
                read_pointer(root)
            self.assertEqual(caught.exception.code, "POINTER_INVALID")
            (root / "_pointer.json").write_text('{"table": "probe"}', encoding="utf-8")
            with self.assertRaises(SnapshotError):
                read_pointer(root)

    def test_load_chain_reads_manifest_history_and_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "probe"
            history = root / "_snapshots"
            history.mkdir(parents=True)
            first = manifest_doc(snapshot_id="s1", sequence=1)
            second = manifest_doc(snapshot_id="s2", parent="s1", sequence=2)
            for doc in (first, second):
                (history / f"{doc['snapshot_id']}.json").write_text(json.dumps(doc), encoding="utf-8")
            (root / "_manifest.json").write_text(json.dumps(second), encoding="utf-8")
            (root / "_pointer.json").write_text('{"snapshot_id": "s1"}', encoding="utf-8")
            value = load_chain(root, "probe", "replica")
            self.assertEqual(value.ids, {"s1", "s2"})
            self.assertEqual(value.pointer, "s1")
            self.assertEqual(value.latest_id, "s2")


class LagAnalysisTests(unittest.TestCase):
    def test_lag_with_evidence(self) -> None:
        primary = chain(
            manifest(snapshot_id="s1", sequence=1),
            manifest(snapshot_id="s2", parent="s1", sequence=2),
            pointer="s2", side="primary",
        )
        replica = chain(manifest(snapshot_id="s1", sequence=1), pointer="s1")
        findings = analyze_chains(primary, replica)
        lag = next(item for item in findings if item.code == "SNAPSHOT_LAG")
        self.assertEqual(lag.severity, "high")
        self.assertEqual(lag.evidence["snapshot_count_delta"], 1)
        self.assertEqual(lag.evidence["sequence_delta"], 1)
        self.assertIn("controlled repair", lag.suggestion)

    def test_unknown_replica_latest_is_a_fork(self) -> None:
        primary = chain(manifest(snapshot_id="p1", sequence=1), manifest(snapshot_id="p2", parent="p1", sequence=2), pointer="p2", side="primary")
        replica = chain(manifest(snapshot_id="r9", sequence=2), pointer="r9")
        findings = analyze_chains(primary, replica)
        self.assertIn("SNAPSHOT_FORK", [item.code for item in findings])
        self.assertNotIn("SNAPSHOT_LAG", [item.code for item in findings])

    def test_stale_pointer(self) -> None:
        primary = chain(manifest(snapshot_id="s1", sequence=1), pointer="s1", side="primary")
        replica = chain(manifest(snapshot_id="s1", sequence=1), pointer=None)
        findings = analyze_chains(primary, replica)
        self.assertEqual([item.code for item in findings], ["POINTER_STALE"])
        self.assertEqual(findings[0].severity, "medium")
        self.assertEqual(findings[0].actual, {"snapshot_id": None})

    def test_broken_primary_chain_blocks_repair(self) -> None:
        primary = chain(manifest(snapshot_id="p2", parent="missing", sequence=2), pointer="p2", side="primary")
        replica = chain(manifest(snapshot_id="p2", parent="missing", sequence=2), pointer="p2")
        findings = analyze_chains(primary, replica)
        self.assertEqual([item.code for item in findings], ["SNAPSHOT_CHAIN_BROKEN", "SNAPSHOT_CHAIN_BROKEN"])
        self.assertTrue(all(item.severity == "critical" for item in findings))

    def test_equal_chains_produce_no_findings(self) -> None:
        docs = (manifest(snapshot_id="s1", sequence=1),)
        primary = chain(*docs, pointer="s1", side="primary")
        replica = chain(*docs, pointer="s1")
        self.assertEqual(analyze_chains(primary, replica), [])


if __name__ == "__main__":
    unittest.main()
