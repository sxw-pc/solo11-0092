import json
import shutil
import tempfile
import unittest
from pathlib import Path

from lakemirror.erasure_evidence import inspect_erasure_manifest
from lakemirror.util import LakeMirrorError


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/erasure_demo"


class ErasureEvidenceTests(unittest.TestCase):
    def test_real_shards_are_verified_but_file_is_not_reconstructed(self):
        result = inspect_erasure_manifest(FIXTURE / "manifest.json")
        self.assertEqual(result["table"], "orders")
        self.assertEqual([x["index"] for x in result["present_shards"]], [0,1,3,4])
        self.assertEqual(result["missing_shards"], [2])
        self.assertEqual(result["first_byte_conflict_witness"],
                         {"byte_offset": 0, "shard4_observed": 0, "shard4_predicted": 1})
        self.assertFalse(result["complete_file_present"])
        self.assertIsNone(result["recovered_bytes"])
        self.assertEqual(len(result["file_sha256"]), 64)
        self.assertEqual(len(result["trusted_manifest_sha256"]), 64)

    def test_bad_shard_bytes_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            for entry in FIXTURE.iterdir():
                shutil.copy2(entry, target / entry.name)
            shard = target / "shard-4.bin"
            data = bytearray(shard.read_bytes())
            data[-1] ^= 1
            shard.write_bytes(data)
            with self.assertRaises(LakeMirrorError):
                inspect_erasure_manifest(target / "manifest.json")

    def test_trusted_snapshot_identity_must_match_shards(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            for entry in FIXTURE.iterdir():
                shutil.copy2(entry, target / entry.name)
            trusted = target / "trusted-file.json"
            value = json.loads(trusted.read_text(encoding="utf-8"))
            value["snapshot_id"] = "another-snapshot"
            trusted.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(LakeMirrorError):
                inspect_erasure_manifest(target / "manifest.json")
