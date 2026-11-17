"""Verify byte-identical but policy-distinct candidate origins for a lake report.

The backfill index has no original producer receipt. This code verifies each
candidate's bytes and metadata without deciding whether to publish.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .util import PathSecurityError, safe_join, sha256_file


class ProvenanceEvidenceError(ValueError):
    pass


def inspect_report_origins(manifest_path: str | Path) -> dict[str, Any]:
    manifest_file = Path(manifest_path).resolve()
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProvenanceEvidenceError("invalid origin manifest") from exc
    if (manifest.get("schema_version") != 2
            or manifest.get("provenance_status") != "ambiguous_backfill"
            or not isinstance(manifest.get("snapshot_id"), str)
            or not isinstance(manifest.get("mandatory_header"), dict)
            or not isinstance(manifest.get("chunks"), list)
            or len(manifest["chunks"]) != 3):
        raise ProvenanceEvidenceError("unsupported origin evidence")

    def checked_file(name: str, digest: str) -> Path:
        if not isinstance(name, str) or not isinstance(digest, str) or len(digest) != 64:
            raise ProvenanceEvidenceError("invalid file identity")
        try:
            file = safe_join(manifest_file.parent, name, must_exist=True)
        except (ValueError, PathSecurityError) as exc:
            raise ProvenanceEvidenceError("invalid origin file path") from exc
        if sha256_file(file) != digest:
            raise ProvenanceEvidenceError("origin file SHA differs")
        return file

    target = checked_file(manifest.get("target_file"), manifest.get("target_sha256"))
    header_info = manifest["mandatory_header"]
    header = checked_file(header_info.get("file"), header_info.get("sha256"))
    chunks = []
    seen = set()
    first_bytes = []
    for chunk in manifest["chunks"]:
        if (not isinstance(chunk, dict) or not isinstance(chunk.get("id"), str)
                or not isinstance(chunk.get("alternatives"), list)
                or len(chunk["alternatives"]) < 2):
            raise ProvenanceEvidenceError("malformed chunk alternatives")
        choices = []
        slot_bytes = None
        for item in chunk["alternatives"]:
            if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                    or item["id"] in seen
                    or item.get("use_class") not in {"external_allowed", "internal_only"}):
                raise ProvenanceEvidenceError("duplicate or malformed origin")
            file = checked_file(item.get("file"), item.get("sha256"))
            data = file.read_bytes()
            if slot_bytes is None:
                slot_bytes = data
            elif data != slot_bytes:
                raise ProvenanceEvidenceError("candidate origin bytes differ within slot")
            seen.add(item["id"])
            choices.append({"id": item["id"], "source_sha256": item["sha256"],
                            "use_class": item["use_class"]})
        first_bytes.append(slot_bytes)
        chunks.append({"id": chunk["id"], "alternatives": choices})
    if header.read_bytes() + b"".join(first_bytes) != target.read_bytes():
        raise ProvenanceEvidenceError("candidate origin bytes do not reconstruct report")
    pairs = manifest.get("forbidden_pairs")
    if (not isinstance(pairs, list) or any(not isinstance(pair, list) or len(pair) != 2
                                             or pair[0] not in seen or pair[1] not in seen
                                             for pair in pairs)):
        raise ProvenanceEvidenceError("invalid cross-origin exclusions")
    contracts = manifest.get("publication_contracts")
    if (not isinstance(contracts, list)
            or {item.get("kind") for item in contracts if isinstance(item, dict)}
            != {"reproducible_authorized", "origin_safe"}
            or any(item.get("audience") != "external" for item in contracts if isinstance(item, dict))):
        raise ProvenanceEvidenceError("publication contracts missing")
    return {
        "snapshot_id": manifest["snapshot_id"],
        "provenance_status": manifest["provenance_status"],
        "manifest_sha256": sha256_file(manifest_file),
        "target_sha256": manifest["target_sha256"],
        "header_sha256": header_info["sha256"],
        "chunks": chunks,
        "forbidden_pairs": pairs,
        "publication_contracts": contracts,
        "publication_decisions": None,
    }
