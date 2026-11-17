from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from lakemirror.provenance_evidence import ProvenanceEvidenceError, inspect_report_origins

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "report_provenance"


def test_ambiguous_backfill_has_byte_identical_policy_distinct_options() -> None:
    evidence = inspect_report_origins(FIXTURE / "manifest.json")
    assert evidence["provenance_status"] == "ambiguous_backfill"
    assert [len(x["alternatives"]) for x in evidence["chunks"]] == [2, 3, 2]
    assert [len({a["source_sha256"] for a in x["alternatives"]}) for x in evidence["chunks"]] == [1, 1, 1]
    assert [a["use_class"] for a in evidence["chunks"][1]["alternatives"]] == [
        "external_allowed", "external_allowed", "internal_only"]
    assert evidence["forbidden_pairs"] == [["name-public", "value-public-a"]]
    assert evidence["publication_decisions"] is None


def test_bad_hash_and_escape_block_origin_evidence(tmp_path: Path) -> None:
    shutil.copytree(FIXTURE, tmp_path / "provenance")
    root = tmp_path / "provenance"
    (root / "chunk1-public.txt").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ProvenanceEvidenceError, match="SHA"):
        inspect_report_origins(root / "manifest.json")
    shutil.copyfile(FIXTURE / "chunk1-public.txt", root / "chunk1-public.txt")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["chunks"][0]["alternatives"][0]["file"] = "../chunk1-public.txt"
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ProvenanceEvidenceError, match="path"):
        inspect_report_origins(root / "manifest.json")
