from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

from lakemirror.provenance_evidence import ProvenanceEvidenceError, inspect_report_origins

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "report_provenance"
MANIFEST = FIXTURE / "manifest.json"


class ProvenanceEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def copy_fixture(self, label: str = "provenance") -> Path:
        root = self.base / label
        shutil.copytree(FIXTURE, root)
        return root

    def mutate_copy(
        self, label: str, mutate: Callable[[dict[str, Any]], None]
    ) -> Path:
        root = self.copy_fixture(label)
        path = root / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        mutate(manifest)
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def assert_blocked(self, path: Path, pattern: str) -> None:
        with self.assertRaisesRegex(ProvenanceEvidenceError, pattern):
            inspect_report_origins(path)

    def test_ambiguous_backfill_has_byte_identical_policy_distinct_options(self) -> None:
        evidence = inspect_report_origins(MANIFEST)
        self.assertEqual(evidence["provenance_status"], "ambiguous_backfill")
        self.assertEqual([len(x["alternatives"]) for x in evidence["chunks"]], [2, 3, 2])
        self.assertEqual(
            [len({a["source_sha256"] for a in x["alternatives"]}) for x in evidence["chunks"]],
            [1, 1, 1])
        self.assertEqual([a["use_class"] for a in evidence["chunks"][1]["alternatives"]],
                         ["external_allowed", "external_allowed", "internal_only"])
        self.assertEqual(evidence["forbidden_pairs"], [["name-public", "value-public-a"]])
        self.assertIsNone(evidence["publication_decisions"])
        self.assertEqual([a["id"] for a in evidence["chunks"][1]["alternatives"]],
                         ["value-public-a", "value-public-b", "value-restricted"])
        self.assertTrue(all(a["source_file"] for chunk in evidence["chunks"]
                            for a in chunk["alternatives"]))
        self.assertEqual(evidence["target_sha256"],
                         "8582601ab567a3fe22e06e8a7fa255e18d91a9cefa09bdf83d0be92bff0c8867")

    def test_bad_hash_and_escape_block_origin_evidence(self) -> None:
        root = self.copy_fixture()
        (root / "chunk1-public.txt").write_text("tampered\n", encoding="utf-8")
        self.assert_blocked(root / "manifest.json", "SHA")
        shutil.copyfile(FIXTURE / "chunk1-public.txt", root / "chunk1-public.txt")
        path = self.mutate_copy(
            "escaped",
            lambda m: m["chunks"][0]["alternatives"][0].__setitem__(
                "file", "../chunk1-public.txt"))
        self.assert_blocked(path, "path")

    def test_missing_or_duplicate_chunks_are_blocked(self) -> None:
        path = self.mutate_copy("two-chunks", lambda m: m["chunks"].pop())
        self.assert_blocked(path, "unsupported")
        path = self.mutate_copy(
            "duplicate-chunk", lambda m: m["chunks"][2].__setitem__("id", m["chunks"][0]["id"]))
        self.assert_blocked(path, "chunk")

    def test_duplicate_candidate_ids_and_aliased_files_are_blocked(self) -> None:
        path = self.mutate_copy(
            "duplicate-candidate",
            lambda m: m["chunks"][1]["alternatives"][1].__setitem__("id", "name-public"))
        self.assert_blocked(path, "duplicate")
        path = self.mutate_copy(
            "aliased-file",
            lambda m: m["chunks"][0]["alternatives"][1].__setitem__(
                "file", m["chunks"][0]["alternatives"][0]["file"]))
        self.assert_blocked(path, "aliases")
        root = self.copy_fixture("case-aliased-file")
        shutil.copyfile(root / "chunk2-public-a.txt", root / "CHUNK2-PUBLIC-A.TXT")
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        manifest["chunks"][1]["alternatives"][1]["file"] = "CHUNK2-PUBLIC-A.TXT"
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.assert_blocked(root / "manifest.json", "aliases")

    def test_candidate_bytes_differing_within_a_slot_are_blocked(self) -> None:
        root = self.copy_fixture("slot-mismatch")
        altered = root / "chunk2-restricted.txt"
        altered.write_bytes(b"8\n")
        digest = hashlib.sha256(altered.read_bytes()).hexdigest()
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        manifest["chunks"][1]["alternatives"][2]["sha256"] = digest
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.assert_blocked(root / "manifest.json", "bytes differ")

    def test_forbidden_pair_referencing_unknown_source_is_blocked(self) -> None:
        path = self.mutate_copy(
            "unknown-pair",
            lambda m: m["forbidden_pairs"].append(["name-public", "footer-missing"]))
        self.assert_blocked(path, "exclusion")
        path = self.mutate_copy(
            "long-pair",
            lambda m: m["forbidden_pairs"].append(["name-public", "footer-public", "x"]))
        self.assert_blocked(path, "exclusion")
        path = self.mutate_copy(
            "same-chunk-pair",
            lambda m: m["forbidden_pairs"].append(["name-public", "name-restricted"]))
        self.assert_blocked(path, "exclusion")
        path = self.mutate_copy(
            "reversed-pair",
            lambda m: m["forbidden_pairs"].append(["value-public-a", "name-public"]))
        self.assert_blocked(path, "duplicate")

    def test_contract_and_snapshot_misbinding_are_blocked(self) -> None:
        path = self.mutate_copy(
            "contract-target",
            lambda m: m["publication_contracts"][0].__setitem__("target_sha256", "0" * 64))
        self.assert_blocked(path, "binding")
        path = self.mutate_copy(
            "contract-snapshot",
            lambda m: m["publication_contracts"][1].__setitem__("snapshot_id", "other"))
        self.assert_blocked(path, "binding")
        path = self.mutate_copy(
            "contract-audience",
            lambda m: m["publication_contracts"][0].__setitem__("audience", "internal"))
        self.assert_blocked(path, "audience")
        path = self.mutate_copy(
            "contract-duplicate-kind",
            lambda m: m["publication_contracts"][1].__setitem__(
                "kind", "reproducible_authorized"))
        self.assert_blocked(path, "contract")

    def test_reconstruction_and_header_mismatch_are_blocked(self) -> None:
        root = self.copy_fixture("reconstruction")
        target = root / "report.txt"
        target.write_bytes(b"report-v1\nnet=\n8\nend\n")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        manifest["target_sha256"] = digest
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.assert_blocked(root / "manifest.json", "reconstruct")
        root = self.copy_fixture("header")
        (root / "header.txt").write_text("report-v2\n", encoding="utf-8")
        self.assert_blocked(root / "manifest.json", "header origin file SHA differs")

    def test_non_string_contract_and_use_class_are_blocked(self) -> None:
        path = self.mutate_copy(
            "list-contract", lambda m: m["publication_contracts"][0].__setitem__("kind", []))
        self.assert_blocked(path, "contract")
        path = self.mutate_copy(
            "list-use-class",
            lambda m: m["chunks"][0]["alternatives"][0].__setitem__("use_class", []))
        self.assert_blocked(path, "malformed")

    def test_missing_contract_kind_is_blocked(self) -> None:
        path = self.mutate_copy("missing-contract", lambda m: m["publication_contracts"].pop())
        self.assert_blocked(path, "contract")


if __name__ == "__main__":
    unittest.main()
