"""Verify byte-identical but policy-distinct candidate origins for a lake report.

The backfill index has no original producer receipt. This code verifies each
candidate's bytes and metadata without deciding whether to publish. Identical
bytes are never treated as proof of identical authorization identity.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .util import (
    LakeMirrorError,
    PathSecurityError,
    is_valid_sha256,
    safe_join,
    sha256_file,
)

USE_CLASSES = frozenset({"external_allowed", "internal_only"})
CONTRACT_KINDS = frozenset({"reproducible_authorized", "origin_safe"})


class ProvenanceEvidenceError(LakeMirrorError):
    """Raised when synthetic report origin evidence is invalid or incomplete."""

    def __init__(self, message: str, code: str = "PROVENANCE_EVIDENCE_INVALID") -> None:
        super().__init__(message, code)


@dataclass(frozen=True)
class OriginCandidate:
    id: str
    source_file: str
    source_sha256: str
    use_class: str
    data: bytes


@dataclass(frozen=True)
class OriginChunk:
    id: str
    alternatives: tuple[OriginCandidate, ...]


@dataclass(frozen=True)
class ReportOrigins:
    manifest_path: Path
    manifest_sha256: str
    snapshot_id: str
    provenance_status: str
    target_file: str
    target_sha256: str
    target_data: bytes
    header_file: str
    header_sha256: str
    header_data: bytes
    chunks: tuple[OriginChunk, ...]
    forbidden_pairs: tuple[tuple[str, str], ...]
    contracts: tuple[dict[str, Any], ...]


def _checked_file(root: Path, name: Any, digest: Any, *, role: str) -> tuple[str, Path]:
    if (not isinstance(name, str) or not isinstance(digest, str)
            or len(digest) != 64 or not is_valid_sha256(digest)):
        raise ProvenanceEvidenceError(
            f"invalid file identity for {role}", "PROVENANCE_IDENTITY_INVALID")
    try:
        file = safe_join(root, name, must_exist=True)
    except (ValueError, PathSecurityError) as exc:
        raise ProvenanceEvidenceError(
            f"invalid {role} path", "PROVENANCE_PATH_INVALID") from exc
    if sha256_file(file) != digest:
        raise ProvenanceEvidenceError(
            f"{role} origin file SHA differs", "PROVENANCE_SHA_MISMATCH")
    return name, file


def _contracts(manifest: dict[str, Any], manifest_sha256: str) -> tuple[dict[str, Any], ...]:
    raw = manifest.get("publication_contracts")
    if not isinstance(raw, list) or len(raw) != 2:
        raise ProvenanceEvidenceError(
            "publication contracts missing", "PROVENANCE_CONTRACT_INVALID")
    contracts: list[dict[str, Any]] = []
    seen_kinds: set[str] = set()
    seen_ids: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ProvenanceEvidenceError(
                "malformed publication contract", "PROVENANCE_CONTRACT_INVALID")
        kind = item.get("kind")
        if (not isinstance(kind, str) or kind not in CONTRACT_KINDS
                or kind in seen_kinds):
            raise ProvenanceEvidenceError(
                "duplicate or unsupported publication contract", "PROVENANCE_CONTRACT_INVALID")
        if item.get("audience") != "external":
            raise ProvenanceEvidenceError(
                "publication contract audience is not external", "PROVENANCE_CONTRACT_INVALID")
        contract_id = item.get("contract_id")
        if contract_id is not None:
            if not isinstance(contract_id, str) or contract_id in seen_ids:
                raise ProvenanceEvidenceError(
                    "duplicate publication contract id", "PROVENANCE_CONTRACT_INVALID")
            seen_ids.add(contract_id)
        bindings = {
            "snapshot_id": manifest.get("snapshot_id"),
            "target_file": manifest.get("target_file"),
            "target_sha256": manifest.get("target_sha256"),
            "provenance_status": manifest.get("provenance_status"),
            "manifest_sha256": manifest_sha256,
        }
        for field, expected in bindings.items():
            if field in item and item[field] != expected:
                raise ProvenanceEvidenceError(
                    f"publication contract {field} binding differs",
                    "PROVENANCE_CONTRACT_MISBOUND")
        seen_kinds.add(kind)
        contracts.append(dict(item))
    return tuple(contracts)


def load_report_origins(manifest_path: str | Path) -> ReportOrigins:
    """Load and fully verify origin evidence, keeping candidate bytes internal."""

    manifest_file = Path(manifest_path).resolve()
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        manifest_sha256 = sha256_file(manifest_file)
    except (OSError, ValueError) as exc:
        raise ProvenanceEvidenceError("invalid origin manifest", "PROVENANCE_MANIFEST_INVALID") from exc
    if not isinstance(manifest, dict):
        raise ProvenanceEvidenceError("invalid origin manifest", "PROVENANCE_MANIFEST_INVALID")
    if (manifest.get("schema_version") != 2
            or manifest.get("provenance_status") != "ambiguous_backfill"
            or not isinstance(manifest.get("snapshot_id"), str)
            or not manifest["snapshot_id"]
            or not isinstance(manifest.get("mandatory_header"), dict)
            or not isinstance(manifest.get("chunks"), list)
            or len(manifest["chunks"]) != 3):
        raise ProvenanceEvidenceError(
            "unsupported origin evidence", "PROVENANCE_MANIFEST_INVALID")

    target_file, target = _checked_file(
        manifest_file.parent, manifest.get("target_file"),
        manifest.get("target_sha256"), role="target")
    header_info = manifest["mandatory_header"]
    header_file, header = _checked_file(
        manifest_file.parent, header_info.get("file"),
        header_info.get("sha256"), role="header")

    seen_ids: set[str] = set()
    seen_chunks: set[str] = set()
    # A source file may carry only one declared identity. Distinct files with
    # equal content remain distinct authorization identities.
    seen_files = {target_file, header_file}
    casefolded_files = {target_file.casefold(), header_file.casefold()}
    chunk_id_of: dict[str, int] = {}
    chunks: list[OriginChunk] = []
    first_bytes: list[bytes] = []
    for index, chunk in enumerate(manifest["chunks"]):
        if (not isinstance(chunk, dict) or not isinstance(chunk.get("id"), str)
                or not chunk["id"] or chunk["id"] in seen_chunks
                or not isinstance(chunk.get("alternatives"), list)
                or len(chunk["alternatives"]) < 2):
            raise ProvenanceEvidenceError(
                "malformed or duplicate chunk", "PROVENANCE_CHUNK_INVALID")
        seen_chunks.add(chunk["id"])
        choices: list[OriginCandidate] = []
        slot_bytes: bytes | None = None
        for item in chunk["alternatives"]:
            if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                    or not item["id"] or item["id"] in seen_ids
                    or not isinstance(item.get("use_class"), str)
                    or item["use_class"] not in USE_CLASSES):
                raise ProvenanceEvidenceError(
                    "duplicate or malformed origin", "PROVENANCE_CANDIDATE_INVALID")
            source_file, file = _checked_file(
                manifest_file.parent, item.get("file"), item.get("sha256"),
                role=f"candidate {item['id']}")
            if source_file in seen_files or source_file.casefold() in casefolded_files:
                raise ProvenanceEvidenceError(
                    "candidate file aliases another source identity",
                    "PROVENANCE_SOURCE_ALIASED")
            seen_files.add(source_file)
            casefolded_files.add(source_file.casefold())
            try:
                data = file.read_bytes()
            except OSError as exc:
                raise ProvenanceEvidenceError(
                    "candidate origin is unreadable", "PROVENANCE_CANDIDATE_INVALID") from exc
            if slot_bytes is None:
                slot_bytes = data
            elif data != slot_bytes:
                raise ProvenanceEvidenceError(
                    "candidate origin bytes differ within slot",
                    "PROVENANCE_SLOT_BYTES_DIFFER")
            seen_ids.add(item["id"])
            chunk_id_of[item["id"]] = index
            choices.append(OriginCandidate(
                id=item["id"], source_file=source_file,
                source_sha256=item["sha256"], use_class=item["use_class"], data=data))
        first_bytes.append(slot_bytes if slot_bytes is not None else b"")
        chunks.append(OriginChunk(id=chunk["id"], alternatives=tuple(choices)))

    if header.read_bytes() + b"".join(first_bytes) != target.read_bytes():
        raise ProvenanceEvidenceError(
            "candidate origin bytes do not reconstruct report",
            "PROVENANCE_RECONSTRUCTION_FAILED")

    raw_pairs = manifest.get("forbidden_pairs")
    pairs: list[tuple[str, str]] = []
    seen_pairs: list[frozenset[str]] = []
    if not isinstance(raw_pairs, list):
        raise ProvenanceEvidenceError(
            "invalid cross-origin exclusions", "PROVENANCE_PAIR_INVALID")
    for pair in raw_pairs:
        if (not isinstance(pair, list) or len(pair) != 2
                or not all(isinstance(value, str) for value in pair)
                or pair[0] not in seen_ids or pair[1] not in seen_ids
                or pair[0] == pair[1]
                or chunk_id_of[pair[0]] == chunk_id_of[pair[1]]):
            raise ProvenanceEvidenceError(
                "invalid cross-origin exclusion", "PROVENANCE_PAIR_INVALID")
        normalized = tuple(pair)
        key = frozenset(normalized)
        if key in seen_pairs:
            raise ProvenanceEvidenceError(
                "duplicate cross-origin exclusion", "PROVENANCE_PAIR_INVALID")
        seen_pairs.append(key)
        pairs.append(normalized)

    return ReportOrigins(
        manifest_path=manifest_file,
        manifest_sha256=manifest_sha256,
        snapshot_id=manifest["snapshot_id"],
        provenance_status=manifest["provenance_status"],
        target_file=target_file,
        target_sha256=manifest["target_sha256"],
        target_data=target.read_bytes(),
        header_file=header_file,
        header_sha256=header_info["sha256"],
        header_data=header.read_bytes(),
        chunks=tuple(chunks),
        forbidden_pairs=tuple(pairs),
        contracts=_contracts(manifest, manifest_sha256),
    )


def inspect_report_origins(manifest_path: str | Path) -> dict[str, Any]:
    """Return stable, reviewable origin evidence without publication decisions."""

    origins = load_report_origins(manifest_path)
    return {
        "snapshot_id": origins.snapshot_id,
        "provenance_status": origins.provenance_status,
        "manifest_sha256": origins.manifest_sha256,
        "target_sha256": origins.target_sha256,
        "header_sha256": origins.header_sha256,
        "chunks": [
            {
                "id": chunk.id,
                "alternatives": [
                    {
                        "id": candidate.id,
                        "source_file": candidate.source_file,
                        "source_sha256": candidate.source_sha256,
                        "use_class": candidate.use_class,
                    }
                    for candidate in chunk.alternatives
                ],
            }
            for chunk in origins.chunks
        ],
        "forbidden_pairs": [list(pair) for pair in origins.forbidden_pairs],
        "publication_contracts": [dict(contract) for contract in origins.contracts],
        "publication_decisions": None,
    }
