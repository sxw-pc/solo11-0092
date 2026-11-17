"""Configuration validation and fingerprint tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lakemirror.config import load_config
from lakemirror.config_types import ConfigError
from tests.support import config_data, make_config, write_config, workspace


class ConfigTests(unittest.TestCase):
    def test_valid_config_and_stable_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = make_config(tmp)
            second = load_config(first.config_path)
            self.assertEqual(first.fingerprint, second.fingerprint)
            self.assertEqual(set(first.tables), {"events", "orders"})
            self.assertTrue(first.dry_run_default)
            self.assertTrue(first.replica_root.is_relative_to(first.workdir))
            self.assertIn("auto_fix", first.snapshot()["repair"])

    def test_schema_version_must_be_supported_integer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = workspace(tmp)
            for index, value in enumerate((2, "1", True, None)):
                data = config_data(work)
                data["schema_version"] = value
                with self.assertRaises(ConfigError) as caught:
                    load_config(write_config(tmp, data, f"version-{index}.json"))
                self.assertIn("schema_version", str(caught.exception))

    def test_unknown_conflict_category_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = workspace(tmp)
            data = config_data(work)
            data["repair"]["auto_fix"] = ["NOT_A_CONFLICT"]
            with self.assertRaises(ConfigError) as caught:
                load_config(write_config(tmp, data))
            self.assertIn("unknown conflict category", str(caught.exception))

    def test_duplicate_table_name_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = workspace(tmp)
            data = config_data(work)
            data["tables"].append(dict(data["tables"][0]))
            with self.assertRaises(ConfigError) as caught:
                load_config(write_config(tmp, data))
            self.assertIn("duplicate table", str(caught.exception))

    def test_replica_must_stay_inside_workdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = workspace(tmp)
            outside = Path(tmp).resolve() / "outside"
            outside.mkdir()
            data = config_data(work)
            data["replica_root"] = str(outside)
            with self.assertRaises(ConfigError) as caught:
                load_config(write_config(tmp, data))
            self.assertIn("replica_root must stay inside workdir", str(caught.exception))

    def test_table_path_escape_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = workspace(tmp)
            data = config_data(work)
            data["tables"][0]["path"] = "../outside"
            with self.assertRaises(ConfigError) as caught:
                load_config(write_config(tmp, data))
            self.assertIn("unsafe path", str(caught.exception))

    def test_unknown_field_and_manual_auto_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = workspace(tmp)
            data = config_data(work)
            data["extra"] = True
            with self.assertRaises(ConfigError):
                load_config(write_config(tmp, data, "unknown.json"))
            data = config_data(work)
            data["repair"]["manual_review"].append("FILE_MISSING_IN_REPLICA")
            with self.assertRaises(ConfigError) as caught:
                load_config(write_config(tmp, data, "overlap.json"))
            self.assertIn("both auto and manual", str(caught.exception))

    def test_ignore_rules_are_loaded_and_audited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rule = {"code": "FILE_EXTRA_IN_REPLICA", "table": "events",
                    "path_prefix": "data/day=2024-02-09", "reason": "temporary exclusion"}
            config = make_config(tmp, ignore_rules=[rule])
            self.assertEqual(len(config.ignore_rules), 1)
            self.assertEqual(config.snapshot()["ignore_rules"][0]["reason"], "temporary exclusion")
            raw = json.loads(config.config_path.read_text(encoding="utf-8"))
            self.assertEqual(raw["ignore_rules"][0]["code"], "FILE_EXTRA_IN_REPLICA")

    def test_dry_run_default_cannot_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = workspace(tmp)
            data = config_data(work)
            data["repair"]["safety"]["dry_run_default"] = False
            with self.assertRaises(ConfigError) as caught:
                load_config(write_config(tmp, data))
            self.assertIn("dry_run_default must be true", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
