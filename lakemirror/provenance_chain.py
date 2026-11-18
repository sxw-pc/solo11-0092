"""Pure reconstruction-chain helpers shared by provenance decisions and receipts."""

from __future__ import annotations

import itertools
from typing import Iterable, Sequence

from .provenance_evidence import OriginCandidate, ReportOrigins
from .util import LakeMirrorError, sha256_bytes

ORIGINAL = "original_backfill_instance"
RECONSTRUCTED = "reconstructed_report_instance"


class ProvenanceReleaseError(LakeMirrorError):
    """Raised when a reconstruction chain or receipt cannot be verified."""

    def __init__(self, message: str, code: str = "PROVENANCE_RELEASE_INVALID") -> None:
        super().__init__(message, code)


def all_combinations(origins: ReportOrigins) -> Iterable[tuple[OriginCandidate, ...]]:
    return itertools.product(*(chunk.alternatives for chunk in origins.chunks))


def combination_checks(
    origins: ReportOrigins, choices: Sequence[OriginCandidate]
) -> tuple[bytes, str, list[list[str]]]:
    data = origins.header_data + b"".join(candidate.data for candidate in choices)
    digest = sha256_bytes(data)
    ids = {candidate.id for candidate in choices}
    hits = [[left, right] for left, right in origins.forbidden_pairs
            if left in ids and right in ids]
    return data, digest, hits


def is_clean(origins: ReportOrigins, choices: Sequence[OriginCandidate]) -> bool:
    _data, digest, hits = combination_checks(origins, choices)
    return (digest == origins.target_sha256
            and all(candidate.use_class == "external_allowed" for candidate in choices)
            and not hits)


def source_entries(
    origins: ReportOrigins, choices: Sequence[OriginCandidate]
) -> list[dict[str, str]]:
    return [
        {
            "chunk_id": chunk.id,
            "candidate_id": candidate.id,
            "source_file": candidate.source_file,
            "source_sha256": candidate.source_sha256,
            "use_class": candidate.use_class,
        }
        for chunk, candidate in zip(origins.chunks, choices)
    ]


def combination_evidence(
    origins: ReportOrigins, choices: Sequence[OriginCandidate]
) -> dict[str, object]:
    data, digest, hits = combination_checks(origins, choices)
    return {
        "source_ids": [candidate.id for candidate in choices],
        "chosen_sources": source_entries(origins, choices),
        "manifest_sha256": origins.manifest_sha256,
        "target_sha256": origins.target_sha256,
        "forbidden_pairs_checked": [list(pair) for pair in origins.forbidden_pairs],
        "forbidden_pair_hits": hits,
        "reconstruction_sha256": digest,
        "byte_equal": data == origins.target_data and digest == origins.target_sha256,
    }


def receipt_choices(
    origins: ReportOrigins, receipt: dict[str, object]
) -> list[OriginCandidate]:
    """Resolve receipt source IDs to the exact declared identities (never by SHA)."""

    entries = receipt.get("chosen_sources")
    if not isinstance(entries, list) or len(entries) != len(origins.chunks):
        raise ProvenanceReleaseError("incomplete source receipt", "CHAIN_INCOMPLETE")
    chosen: list[OriginCandidate] = []
    seen: set[str] = set()
    for chunk, entry in zip(origins.chunks, entries):
        if not isinstance(entry, dict):
            raise ProvenanceReleaseError("malformed source receipt", "RELEASE_RECEIPT_INVALID")
        if entry.get("chunk_id") != chunk.id:
            raise ProvenanceReleaseError("receipt chunk is misbound", "CHUNK_MISBOUND")
        candidate_id = entry.get("candidate_id")
        if not isinstance(candidate_id, str) or candidate_id in seen:
            raise ProvenanceReleaseError("duplicate or unknown source id", "SOURCE_ID_DUPLICATE")
        matches = [candidate for candidate in chunk.alternatives
                   if candidate.id == candidate_id]
        if not matches:
            raise ProvenanceReleaseError("unknown source id", "SOURCE_ID_UNKNOWN")
        candidate = matches[0]
        if (entry.get("source_file") != candidate.source_file
                or entry.get("source_sha256") != candidate.source_sha256
                or entry.get("use_class") != candidate.use_class):
            raise ProvenanceReleaseError(
                "same-digest source substitution rejected", "SOURCE_SUBSTITUTED")
        seen.add(candidate_id)
        chosen.append(candidate)
    return chosen
