"""Fork evidence, dual-parent merge contracts, atomic publication, and reconciliation."""

from __future__ import annotations

import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from lakemirror.fork_evidence import inspect_fork_contract, load_fork_bundle
from lakemirror.fork_publish import publish_fork_merge
from lakemirror.manifest import (
    load_manifest,
    manifest_canonical_text,
    manifest_digest,
)
from lakemirror.reconcile import reconcile_fork_table
from lakemirror.snapshot import build_chain
from lakemirror.util import LakeMirrorError, sha256_bytes

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "fork_merge"


class ForkContractEvaluationTests(unittest.TestCase):
    def test_positive_fork_two_contracts_diverge(self) -> None:
        result = inspect_fork_contract(FIXTURES / "contract.json")
        self.assertTrue(result["chain_forked"])
        self.assertFalse(result["chain_fork_resolved"])
        self.assertEqual(
            [issue["code"] for issue in result["chain_issues"]],
            ["SNAPSHOT_FORK"],
        )
        self.assertEqual(
            result["fork_events"][0]["branch_ids"],
            ["S1", "S2"],
        )
        self.assertIsNone(result["fork_events"][0]["resolved_by"])
        verdicts = result["merge_verdicts"]
        self.assertFalse(verdicts["file_strict"]["allowed"])
        self.assertEqual(
            [block["code"] for block in verdicts["file_strict"]["blocks"]],
            ["FILE_STRICT_BOTH_SIDES_CHANGED"],
        )
        self.assertTrue(verdicts["field_disjoint"]["allowed"])
        rows = {row["order_id"]: row for row in verdicts["field_disjoint"]["rows"]}
        self.assertEqual((rows["O1"]["amount"], rows["O1"]["status"]), ("120", "approved"))
        self.assertEqual((rows["O2"]["amount"], rows["O2"]["status"]), ("50", "open"))
        sources = {
            (item["key"], item["column"]): item["source"]
            for item in verdicts["field_disjoint"]["provenance"]
        }
        self.assertEqual(sources[("O1", "amount")], "primary")
        self.assertEqual(sources[("O1", "status")], "replica")
        self.assertEqual(sources[("O2", "amount")], "ancestor")

    def test_field_conflict_blocks_field_disjoint(self) -> None:
        result = inspect_fork_contract(FIXTURES / "field-conflict-contract.json")
        verdicts = result["merge_verdicts"]
        self.assertFalse(verdicts["file_strict"]["allowed"])
        field = verdicts["field_disjoint"]
        self.assertFalse(field["allowed"])
        block = field["blocks"][0]
        self.assertEqual(block["code"], "MERGE_FIELD_CONFLICT")
        self.assertEqual((block["key"], block["column"]), ("O1", "amount"))

    def test_delete_versus_update_blocks_both_contracts(self) -> None:
        result = inspect_fork_contract(FIXTURES / "conflict-contract.json")
        self.assertNotIn("O2", result["rows_by_snapshot"]["S1D"])
        self.assertEqual(result["rows_by_snapshot"]["S2U"]["O2"]["status"], "closed")
        for name, verdict in result["merge_verdicts"].items():
            self.assertFalse(verdict["allowed"], name)
        codes = {block["code"] for block in result["merge_verdicts"]["field_disjoint"]["blocks"]}
        self.assertIn("MERGE_DELETE_VS_UPDATE", codes)

    def test_duplicate_business_key_is_rejected_at_load(self) -> None:
        with self.assertRaises(LakeMirrorError) as caught:
            inspect_fork_contract(FIXTURES / "duplicate-key-contract.json")
        self.assertEqual(caught.exception.code, "FORK_KEY_INVALID")

    def test_contracts_cannot_borrow_each_others_verdict(self) -> None:
        result = inspect_fork_contract(FIXTURES / "contract.json")
        strict, disjoint = result["merge_verdicts"]["file_strict"], result["merge_verdicts"]["field_disjoint"]
        self.assertNotEqual(strict["allowed"], disjoint["allowed"])
        self.assertEqual(strict["rows"], [])
        self.assertTrue(disjoint["rows"])
        self.assertNotEqual(
            json.dumps(strict["fingerprint_basis"], sort_keys=True),
            json.dumps(disjoint["fingerprint_basis"], sort_keys=True),
        )


class ForkBundleIntegrityTests(unittest.TestCase):
    def test_missing_file_hash_mismatch_and_unknown_ancestor(self) -> None:
        cases = {
            "missing_file/contract.json": "FORK_FILE_MISSING",
            "contract-hash-mismatch.json": "FORK_FILE_HASH_MISMATCH",
            "contract-unknown-ancestor.json": "FORK_NOT_SIBLINGS",
        }
        for rel, code in cases.items():
            with self.subTest(rel=rel):
                with self.assertRaises(LakeMirrorError) as caught:
                    load_fork_bundle(FIXTURES / "corrupt" / rel)
                self.assertEqual(caught.exception.code, code)

    def test_manifest_and_file_evidence_are_recorded(self) -> None:
        bundle = load_fork_bundle(FIXTURES / "contract.json")
        for item in bundle.evidence:
            self.assertEqual(len(item["manifest_sha256"]), 64)
            self.assertEqual(len(item["file_sha256"]), 64)
            self.assertEqual(item["row_count"], len(item["rows"]))


class ForkPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.sandbox = Path(self._tmp.name) / "sandbox"
        self.sandbox.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_file_strict_blocks_publish_without_writing(self) -> None:
        result = publish_fork_merge(
            FIXTURES / "contract.json", "file_strict", self.sandbox, execute=True
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["blocks"][0]["code"], "FILE_STRICT_BOTH_SIDES_CHANGED")
        self.assertEqual(list(self.sandbox.rglob("*")), [])

    def test_field_disjoint_publishes_dual_parent_snapshot_and_verifies(self) -> None:
        result = publish_fork_merge(
            FIXTURES / "contract.json", "field_disjoint", self.sandbox, execute=True
        )
        self.assertEqual(result["status"], "published")
        table_dir = self.sandbox / "orders"
        merged_csv = table_dir / "orders-S3.csv"
        rows = list(csv.DictReader(merged_csv.read_text(encoding="utf-8").splitlines()))
        self.assertEqual(rows[0], {"order_id": "O1", "amount": "120", "status": "approved"})
        self.assertEqual(rows[1], {"order_id": "O2", "amount": "50", "status": "open"})
        manifest = load_manifest(table_dir / "_snapshots" / "S3.json", strict=True)
        self.assertEqual(manifest.parent_ids(), ("S1", "S2"))
        self.assertEqual(manifest.parent_snapshot_id, "S1")
        self.assertEqual(manifest.merge.contract, "field_disjoint")
        self.assertEqual(manifest.merge.ancestor.snapshot_id, "S0")
        self.assertEqual(
            [ref.snapshot_id for ref in manifest.merge.parents],
            ["S1", "S2"],
        )
        for ref in (manifest.merge.ancestor, *manifest.merge.parents):
            self.assertTrue(ref.files and ref.manifest_sha256)
        reread = sha256_bytes(merged_csv.read_bytes())
        self.assertEqual(reread, manifest.files[0].sha256)
        chain = build_chain(
            [load_manifest(p) for p in sorted((table_dir / "_snapshots").glob("*.json"))],
            "orders",
            "combined",
            json.loads((table_dir / "_pointer.json").read_text(encoding="utf-8"))["snapshot_id"],
        )
        pointer = json.loads((table_dir / "_pointer.json").read_text(encoding="utf-8"))
        self.assertEqual(pointer["snapshot_id"], "S3")
        self.assertFalse(chain.forked)
        self.assertTrue(chain.fork_resolved)
        self.assertFalse(chain.broken)
        self.assertEqual(chain.fork_events[0].resolved_by, "S3")
        self.assertIn("SNAPSHOT_FORK", [issue.code for issue in chain.issues])
        verdict = reconcile_fork_table(table_dir, "orders")
        self.assertEqual(verdict["decision"], "readable")
        self.assertEqual(verdict["blocking_issues"], [])

    def test_replay_is_idempotent_but_a_different_plan_is_rejected(self) -> None:
        first = publish_fork_merge(
            FIXTURES / "contract.json", "field_disjoint", self.sandbox, execute=True
        )
        second = publish_fork_merge(
            FIXTURES / "contract.json", "field_disjoint", self.sandbox, execute=True
        )
        self.assertEqual(second["status"], "already_published")
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(first["manifest_sha256"], second["manifest_sha256"])
        snapshots = list((self.sandbox / "orders" / "_snapshots").glob("S3.json"))
        self.assertEqual(len(snapshots), 1)
        divergent_base = Path(self._tmp.name) / "divergent"
        divergent_root = divergent_base / "fixtures" / "fork_merge"
        divergent_root.mkdir(parents=True)
        shutil.copytree(FIXTURES, divergent_root, dirs_exist_ok=True)
        for nested in ("corrupt",):
            target = divergent_root / nested
            if target.exists():
                shutil.rmtree(target)
        self._make_divergent_s1(divergent_root)
        with self.assertRaises(LakeMirrorError) as caught:
            publish_fork_merge(
                divergent_root / "contract.json",
                "field_disjoint",
                self.sandbox,
                execute=True,
            )
        self.assertEqual(caught.exception.code, "FORK_PLAN_CONFLICT")

    @staticmethod
    def _make_divergent_s1(root: Path) -> None:
        csv_path = root / "orders-S1.csv"
        rows = [("O1", 999, "hold"), ("O2", 50, "open")]
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(["order_id", "amount", "status"])
            writer.writerows(rows)
        data = csv_path.read_bytes()
        manifest_path = root / "S1.json"
        doc = json.loads(manifest_path.read_text(encoding="utf-8"))
        doc["files"][0].update(
            size=len(data), row_count=len(rows), sha256=sha256_bytes(data)
        )
        manifest_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    def test_conflict_fixture_never_publishes(self) -> None:
        for contract in ("file_strict", "field_disjoint"):
            result = publish_fork_merge(
                FIXTURES / "conflict-contract.json",
                contract,
                self.sandbox,
                execute=True,
            )
            self.assertEqual(result["status"], "blocked", contract)
        self.assertEqual(list(self.sandbox.rglob("*")), [])

    def test_dry_run_writes_nothing(self) -> None:
        result = publish_fork_merge(
            FIXTURES / "contract.json", "field_disjoint", self.sandbox, execute=False
        )
        self.assertEqual(result["status"], "planned")
        self.assertIn("would_write", result)
        self.assertEqual(list(self.sandbox.rglob("*")), [])

    def test_published_manifest_round_trips_through_canonical_digest(self) -> None:
        publish_fork_merge(
            FIXTURES / "contract.json", "field_disjoint", self.sandbox, execute=True
        )
        path = self.sandbox / "orders" / "_snapshots" / "S3.json"
        self.assertEqual(manifest_digest(load_manifest(path)), manifest_digest(load_manifest(path)))

    def test_merge_lineage_defect_keeps_the_head_blocked(self) -> None:
        from lakemirror.manifest import MergeLineage, MergeRef, FileEntry
        from lakemirror.util import atomic_write_bytes

        table_dir = self.sandbox / "orders"
        history = table_dir / "_snapshots"
        history.mkdir(parents=True)
        for sid in ("S0", "S1", "S2"):
            source = load_manifest(FIXTURES / f"{sid}.json")
            atomic_write_bytes(
                history / f"{sid}.json",
                manifest_canonical_text(source).encode(),
            )
        head = load_manifest(FIXTURES / "S2.json")
        ancestor = load_manifest(FIXTURES / "S0.json")
        bad_primary = MergeRef(
            "primary", "S1", "a" * 64,
            (FileEntry("x.csv", "dt=1999-01-01", 1, 1, "b" * 64),),
        )
        replica = MergeRef("replica", "S2", "c" * 64, head.files)
        ancestor_ref = MergeRef("ancestor", "S0", "d" * 64, ancestor.files)
        broken = MergeLineage("field_disjoint", ancestor_ref, (bad_primary, replica))
        bad_head = type(head)(
            **{**head.__dict__, "snapshot_id": "S3", "sequence": 4,
               "parent_snapshot_id": "S1", "merge": broken}
        )
        atomic_write_bytes(history / "S3.json", manifest_canonical_text(bad_head).encode())
        atomic_write_bytes(table_dir / "_manifest.json", manifest_canonical_text(bad_head).encode())
        atomic_write_bytes(
            table_dir / "_pointer.json",
            json.dumps({"table": "orders", "snapshot_id": "S3"}).encode(),
        )
        verdict = reconcile_fork_table(table_dir, "orders")
        self.assertEqual(verdict["decision"], "blocked")
        self.assertIn(
            "MERGE_LINEAGE_INVALID",
            [issue["code"] for issue in verdict["blocking_issues"]],
        )




if __name__ == "__main__":
    unittest.main()
