import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lakemirror.erasure_recovery import (
    ORDERS_COLUMNS,
    evaluate_recovery,
    load_erasure_evidence,
    write_recovery,
)
from lakemirror.util import LakeMirrorError, sha256_bytes

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "fixtures/erasure_demo"
CLEAN = ROOT / "fixtures/erasure_recovery_clean"
BAD_DIGEST = ROOT / "fixtures/erasure_recovery_bad_digest"
TRUSTED_SHA = "92372ae197608fc4c9ab9bb9704d8afe050555a99de863117dc8527e77d81b98"
CONFLICT = {"byte_offset": 0, "observed": 0, "predicted": 1, "shard_index": 4}


def copy_fixture(source: Path, target: Path) -> Path:
    for entry in source.iterdir():
        shutil.copy2(entry, target / entry.name)
    return target / "manifest.json"


def decisions(report: dict) -> dict:
    return {item["contract"]: item for item in report["contract_decisions"]}


class ErasureRecoveryTests(unittest.TestCase):
    def test_demo_strict_contract_blocks_on_visible_shard_conflict(self):
        report = evaluate_recovery(DEMO / "manifest.json")
        strict = decisions(report)["strict_all_shards"]
        self.assertEqual(strict["decision"], "blocked")
        self.assertFalse(strict["available_for_verification"])
        self.assertFalse(strict["authorized_to_write"])
        self.assertEqual(strict["reason_codes"], ["VISIBLE_SHARD_CONFLICT"])
        self.assertEqual(strict["conflicts"], [CONFLICT])
        self.assertIsNone(strict["recovered_file_sha256"])

    def test_demo_quorum_recovers_from_013_and_isolates_shard4(self):
        report = evaluate_recovery(DEMO / "manifest.json")
        quorum = decisions(report)["digest_quorum"]
        self.assertEqual(quorum["decision"], "recoverable")
        self.assertTrue(quorum["available_for_verification"])
        self.assertTrue(quorum["authorized_to_write"])
        self.assertEqual(quorum["isolated_shards"], [4])
        self.assertEqual(quorum["recovered_file_sha256"], TRUSTED_SHA)
        self.assertEqual(quorum["matching_candidate_count"], 1)
        matching = next(c for c in report["candidates"]
                        if c["file_sha256"] == TRUSTED_SHA)
        self.assertEqual(matching["source_triples"], [[0, 1, 3]])
        self.assertEqual(tuple(matching["csv_columns"]), ORDERS_COLUMNS)

    def test_every_triple_is_enumerated_and_wrong_ones_are_not_authoritative(self):
        report = evaluate_recovery(DEMO / "manifest.json")
        triples = sorted(tuple(t) for c in report["candidates"] for t in c["source_triples"])
        self.assertEqual(triples, [(0, 1, 3), (0, 1, 4), (0, 3, 4), (1, 3, 4)])
        self.assertEqual(len(report["candidates"]), 4)
        self.assertEqual(sum(1 for c in report["candidates"]
                             if c["file_sha256"] == TRUSTED_SHA), 1)
        # The self-hash of shard4 alone must never make it an encoding-consistent shard.
        for candidate in report["candidates"]:
            if candidate["file_sha256"] != TRUSTED_SHA:
                self.assertTrue(candidate["conflicts"])

    def test_shard4_self_hash_matches_but_still_gets_isolated(self):
        evidence = load_erasure_evidence(DEMO / "manifest.json")
        shard4 = next(row for row in evidence["present_records"] if row["index"] == 4)
        self.assertEqual(sha256_bytes(evidence["present"][4]), shard4["sha256"])
        report = evaluate_recovery(DEMO / "manifest.json")
        self.assertIn(4, decisions(report)["digest_quorum"]["isolated_shards"])

    def test_clean_fixture_allows_both_contracts(self):
        report = evaluate_recovery(CLEAN / "manifest.json")
        for contract, decision in decisions(report).items():
            with self.subTest(contract=contract):
                self.assertEqual(decision["decision"], "recoverable")
                self.assertTrue(decision["authorized_to_write"])
                self.assertEqual(decision["isolated_shards"], [])
                self.assertEqual(decision["recovered_file_sha256"], TRUSTED_SHA)

    def test_untrusted_digest_blocks_both_contracts_without_publishing(self):
        report = evaluate_recovery(BAD_DIGEST / "manifest.json")
        for contract, decision in decisions(report).items():
            with self.subTest(contract=contract):
                self.assertEqual(decision["decision"], "blocked")
                self.assertEqual(decision["reason_codes"],
                                 ["NO_CANDIDATE_MATCHES_TRUSTED_DIGEST"])
                self.assertIsNone(decision["recovered_file_sha256"])

    def test_changing_one_shard_byte_changes_the_availability_conclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = copy_fixture(CLEAN, Path(tmp))
            shard4 = Path(tmp) / "shard-4.bin"
            data = bytearray(shard4.read_bytes())
            data[0] ^= 0x01
            shard4.write_bytes(data)
            # The recorded register SHA no longer matches: evidence must be rejected.
            with self.assertRaises(LakeMirrorError):
                evaluate_recovery(manifest)
            register = json.loads(manifest.read_text(encoding="utf-8"))
            row = next(row for row in register["shards"] if row["index"] == 4)
            row["sha256"] = sha256_bytes(bytes(data))
            manifest.write_text(json.dumps(register), encoding="utf-8")
            report = evaluate_recovery(manifest)
            strict = decisions(report)["strict_all_shards"]
            quorum = decisions(report)["digest_quorum"]
            self.assertEqual(strict["decision"], "blocked")
            self.assertEqual(strict["reason_codes"], ["VISIBLE_SHARD_CONFLICT"])
            self.assertEqual(quorum["decision"], "recoverable")
            self.assertEqual(quorum["isolated_shards"], [4])

    def test_changing_trusted_sha_rejects_previously_valid_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = copy_fixture(CLEAN, Path(tmp))
            trusted_path = Path(tmp) / "trusted-file.json"
            trusted = json.loads(trusted_path.read_text(encoding="utf-8"))
            trusted["file_sha256"] = "f" * 64
            trusted_path.write_text(json.dumps(trusted), encoding="utf-8")
            report = evaluate_recovery(manifest)
            for decision in decisions(report).values():
                self.assertEqual(decision["decision"], "blocked")
                self.assertEqual(decision["reason_codes"],
                                 ["NO_CANDIDATE_MATCHES_TRUSTED_DIGEST"])

    def test_changing_orders_identity_blocks_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = copy_fixture(CLEAN, Path(tmp))
            # Drift the identity only inside the trusted authority; the shard-side
            # manifest keeps the old identity, so the two can no longer be the same file.
            trusted_path = Path(tmp) / "trusted-file.json"
            trusted = json.loads(trusted_path.read_text(encoding="utf-8"))
            trusted["snapshot_id"] = "snap-other"
            trusted_path.write_text(json.dumps(trusted), encoding="utf-8")
            with self.assertRaises(LakeMirrorError) as ctx:
                evaluate_recovery(manifest)
            self.assertEqual(ctx.exception.code, "ERASURE_TRUST")

    def test_quorum_blocks_when_csv_columns_are_not_orders_shape(self):
        from lakemirror.erasure_recovery import (
            SUPPORTED_MATRIX, _evaluate, gmul,
        )
        # A fully self-consistent alternative encoding whose file is a three-column CSV.
        other = b"order_id,customer_id,amount\n10000,account-00,7.00\n"
        width = 22
        padded = other + b"\x00" * (3 * width - len(other))
        data = [padded[k * width:(k + 1) * width] for k in range(3)]
        shards = [bytes(
            gmul(row[0], data[0][j]) ^ gmul(row[1], data[1][j]) ^ gmul(row[2], data[2][j])
            for j in range(width)) for row in SUPPORTED_MATRIX]
        evidence = {
            "manifest_path": Path("/unused/manifest.json"),
            "manifest": {"generator_rows": SUPPORTED_MATRIX,
                         "source_file_rel": "data/dt=2024-01-01/part-erasure.csv"},
            "trusted": {"file_sha256": sha256_bytes(other)},
            "present": {0: shards[0], 1: shards[1], 3: shards[3], 4: shards[4]},
            "present_records": [], "missing": [2], "width": width,
            "original_size": len(other)}
        from lakemirror.erasure_recovery import build_candidates
        candidates = build_candidates(evidence)
        quorum = _evaluate("digest_quorum", evidence, candidates)
        self.assertEqual(quorum["decision"], "blocked")
        self.assertIn("ORDERS_CSV_COLUMNS_MISMATCH", quorum["reason_codes"])
        strict = _evaluate("strict_all_shards", evidence, candidates)
        self.assertEqual(strict["decision"], "recoverable")

    def test_quorum_floor_blocks_when_fewer_than_three_shards_survive_isolation(self):
        from lakemirror.erasure_recovery import _evaluate
        evidence = {
            "manifest_path": Path("/unused/manifest.json"), "manifest": {},
            "trusted": {"file_sha256": "a" * 64}, "present": {0: b"", 1: b"", 3: b"", 4: b""},
            "present_records": [], "missing": [2], "width": 1, "original_size": 1}
        candidate = {"file_sha256": "a" * 64, "original_size": 1, "source_triples": [[0, 1, 3]],
                     "conflicts": [{"shard_index": 3}, {"shard_index": 4}],
                     "csv_columns": list(ORDERS_COLUMNS)}
        quorum = _evaluate("digest_quorum", evidence, [candidate])
        self.assertEqual(quorum["decision"], "blocked")
        self.assertEqual(quorum["reason_codes"], ["INSUFFICIENT_CONSISTENT_SHARDS"])

    def test_write_is_authorized_idempotent_and_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            first = write_recovery(DEMO / "manifest.json", out, "digest_quorum")
            self.assertTrue(first["authorized_to_write"])
            output = Path(first["output_file"])
            self.assertEqual(sha256_bytes(output.read_bytes()), TRUSTED_SHA)
            self.assertEqual(output.relative_to(out.resolve()).as_posix(),
                             "data/dt=2024-01-01/part-erasure.csv")
            second = write_recovery(DEMO / "manifest.json", out, "digest_quorum")
            self.assertTrue(second["idempotent"])
            self.assertEqual(second["output_sha256"], TRUSTED_SHA)

    def test_blocked_contract_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            result = write_recovery(DEMO / "manifest.json", out, "strict_all_shards")
            self.assertFalse(result["authorized_to_write"])
            self.assertIsNone(result["output_file"])
            self.assertFalse(out.exists() and any(out.rglob("*")))

    def test_output_refuses_fixture_overlap_and_external_differing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(LakeMirrorError):
                write_recovery(DEMO / "manifest.json", DEMO / "nested", "digest_quorum")
            out = Path(tmp) / "out"
            target = out / "data/dt=2024-01-01/part-erasure.csv"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"different bytes already here")
            with self.assertRaises(LakeMirrorError):
                write_recovery(DEMO / "manifest.json", out, "digest_quorum")

    def test_evidence_register_cannot_promote_itself_to_file_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = copy_fixture(BAD_DIGEST, Path(tmp))
            register = json.loads(manifest.read_text(encoding="utf-8"))
            candidate = next(c for c in evaluate_recovery(manifest)["candidates"])
            # Rewriting a shard register digest to match a candidate does not authorize it.
            row = register["shards"][0]
            row["sha256"] = candidate["file_sha256"]
            manifest.write_text(json.dumps(register), encoding="utf-8")
            with self.assertRaises(LakeMirrorError):
                evaluate_recovery(manifest)

    def test_cli_reports_both_contracts_and_exit_codes(self):
        strict = subprocess.run(
            [sys.executable, "-m", "lakemirror", "erasure-recover",
             "--manifest", str(DEMO / "manifest.json"), "--contract", "strict_all_shards"],
            cwd=str(ROOT), text=True, capture_output=True, timeout=60)
        self.assertEqual(strict.returncode, 2, strict.stderr)
        self.assertEqual(decisions(json.loads(strict.stdout))["strict_all_shards"]["decision"],
                         "blocked")
        with tempfile.TemporaryDirectory() as tmp:
            quorum = subprocess.run(
                [sys.executable, "-m", "lakemirror", "erasure-recover",
                 "--manifest", str(DEMO / "manifest.json"), "--contract", "digest_quorum",
                 "--output-dir", str(Path(tmp) / "out")],
                cwd=str(ROOT), text=True, capture_output=True, timeout=60)
            self.assertEqual(quorum.returncode, 0, quorum.stderr)
            payload = json.loads(quorum.stdout)
            self.assertEqual(payload["write"]["output_sha256"], TRUSTED_SHA)
            self.assertTrue(Path(payload["write"]["output_file"]).is_file())


if __name__ == "__main__":
    unittest.main()
