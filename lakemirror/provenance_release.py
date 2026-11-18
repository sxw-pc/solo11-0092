"""Adjudicate report publication rights separately from byte reproducibility.

A candidate is authorized as itself, never as another candidate that happens to
share a SHA-256. A clean reconstruction chain can authorize a *new* report
instance; it cannot prove that the unknown original was produced from clean
sources.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from .provenance_chain import (
    ORIGINAL,
    RECONSTRUCTED,
    ProvenanceReleaseError,
    all_combinations,
    combination_checks,
    combination_evidence,
    is_clean,
)
from .provenance_evidence import (
    CONTRACT_KINDS,
    OriginCandidate,
    ReportOrigins,
    load_report_origins,
)


def _restricted_ids(origins: ReportOrigins) -> list[str]:
    return sorted({candidate.id for chunk in origins.chunks
                   for candidate in chunk.alternatives
                   if candidate.use_class != "external_allowed"})


def _reconstructed_receipt(
    origins: ReportOrigins, kind: str, chosen: Sequence[OriginCandidate]
) -> dict[str, Any]:
    evidence = combination_evidence(origins, chosen)
    return {
        "receipt_kind": RECONSTRUCTED,
        "contract_kind": kind,
        "snapshot_id": origins.snapshot_id,
        "manifest_sha256": origins.manifest_sha256,
        "target_file": origins.target_file,
        "target_sha256": origins.target_sha256,
        "chosen_sources": evidence["chosen_sources"],
        "forbidden_pairs_checked": evidence["forbidden_pairs_checked"],
        "forbidden_pair_hits": evidence["forbidden_pair_hits"],
        "reconstruction_sha256": evidence["reconstruction_sha256"],
        "byte_equal": evidence["byte_equal"],
        "original_producer_receipt": False,
    }


def _reproducible_decision(origins: ReportOrigins, contract: dict[str, Any]) -> dict[str, Any]:
    clean: list[tuple[OriginCandidate, ...]] = []
    rejected = 0
    for choices in all_combinations(origins):
        if is_clean(origins, choices):
            clean.append(choices)
        else:
            rejected += 1
    if clean:
        chosen = clean[0]
        return {
            "contract_kind": contract["kind"],
            "audience": contract["audience"],
            "status": "allowed",
            "reason_code": None,
            "publication_kind": RECONSTRUCTED,
            "publication_sha256": origins.target_sha256,
            "selected_combination": combination_evidence(origins, chosen),
            "counterexamples": [],
            "reasons": [],
            "checks": {
                "all_sources_external_allowed": True,
                "no_forbidden_pairs": True,
                "target_bytes_equal": True,
                "chain_complete": True,
            },
            "rejected_combination_count": rejected,
            "source_receipt": _reconstructed_receipt(origins, contract["kind"], chosen),
        }
    reasons: list[dict[str, Any]] = []
    restricted = _restricted_ids(origins)
    if restricted:
        reasons.append({"code": "RESTRICTED_CANDIDATES_PRESENT", "source_ids": restricted})
    reasons.append({"code": "NO_CLEAN_EXTERNAL_COMBINATION",
                    "detail": "no candidate chain is fully external and pair-free"})
    return {
        "contract_kind": contract["kind"],
        "audience": contract["audience"],
        "status": "blocked",
        "reason_code": "NO_AUTHORIZED_RECONSTRUCTION",
        "publication_kind": None,
        "publication_sha256": None,
        "selected_combination": None,
        "counterexamples": [],
        "reasons": reasons,
        "checks": {
            "all_sources_external_allowed": False,
            "no_forbidden_pairs": False,
            "target_bytes_equal": True,
            "chain_complete": True,
        },
        "source_receipt": None,
    }


def _origin_safe_decision(origins: ReportOrigins, contract: dict[str, Any]) -> dict[str, Any]:
    unsafe: list[tuple[OriginCandidate, ...]] = []
    checked = 0
    for choices in all_combinations(origins):
        _data, digest, hits = combination_checks(origins, choices)
        if digest != origins.target_sha256:
            # Not a historical origin compatible with the backfill evidence.
            continue
        checked += 1
        if hits or any(c.use_class != "external_allowed" for c in choices):
            unsafe.append(choices)
    if not unsafe:
        return {
            "contract_kind": contract["kind"],
            "audience": contract["audience"],
            "status": "allowed",
            "reason_code": None,
            "publication_kind": ORIGINAL,
            "publication_sha256": origins.target_sha256,
            "selected_combination": None,
            "counterexamples": [],
            "reasons": [],
            "checks": {
                "all_sources_external_allowed": True,
                "no_forbidden_pairs": True,
                "target_bytes_equal": True,
                "chain_complete": True,
                "all_compatible_origins_checked": True,
            },
            "combinations_checked": checked,
            "source_receipt": {
                "receipt_kind": ORIGINAL,
                "contract_kind": contract["kind"],
                "snapshot_id": origins.snapshot_id,
                "manifest_sha256": origins.manifest_sha256,
                "target_file": origins.target_file,
                "target_sha256": origins.target_sha256,
                "source_ids": [candidate.id for chunk in origins.chunks
                               for candidate in chunk.alternatives],
                "forbidden_pairs_checked": [list(pair) for pair in origins.forbidden_pairs],
                "forbidden_pair_hits": [],
                "reconstruction_sha256": origins.target_sha256,
                "byte_equal": True,
                "combinations_checked": checked,
                "all_compatible_origins_external_allowed": True,
                "forbidden_pairs_absent_in_all_combinations": True,
                "original_producer_receipt": None,
            },
        }
    counterexamples = [combination_evidence(origins, choices) for choices in unsafe]
    restricted = _restricted_ids(origins)
    reasons: list[dict[str, Any]] = []
    if restricted:
        reasons.append({
            "code": "RESTRICTED_HISTORICAL_ORIGIN",
            "source_ids": restricted,
            "detail": "a restricted candidate remains byte-compatible with the original",
        })
    if any(entry["forbidden_pair_hits"] for entry in counterexamples):
        reasons.append({
            "code": "FORBIDDEN_HISTORICAL_PAIR",
            "forbidden_pairs": [list(pair) for pair in origins.forbidden_pairs],
            "detail": "a byte-compatible historical combination violates a forbidden pair",
        })
    return {
        "contract_kind": contract["kind"],
        "audience": contract["audience"],
        "status": "blocked",
        "reason_code": "ORIGIN_NOT_PROVABLY_SAFE",
        "publication_kind": None,
        "publication_sha256": None,
        "selected_combination": None,
        "counterexamples": counterexamples,
        "reasons": reasons,
        "checks": {
            "all_sources_external_allowed": False,
            "no_forbidden_pairs": False,
            "target_bytes_equal": True,
            "chain_complete": True,
            "all_compatible_origins_checked": True,
        },
        "combinations_checked": checked,
        "source_receipt": None,
    }


def adjudicate_report_release(
    manifest_path: str | Path, *, contract_kinds: Sequence[str] | None = None
) -> dict[str, Any]:
    """Return per-contract publication decisions with reviewable evidence."""

    origins = load_report_origins(manifest_path)
    if contract_kinds is not None and not contract_kinds:
        raise ProvenanceReleaseError("empty publication contract request",
                                     "PROVENANCE_CONTRACT_INVALID")
    requested = set(contract_kinds) if contract_kinds else set(CONTRACT_KINDS)
    if not requested <= CONTRACT_KINDS:
        raise ProvenanceReleaseError("unsupported publication contract request",
                                     "PROVENANCE_CONTRACT_INVALID")
    available = {contract["kind"] for contract in origins.contracts}
    if not requested <= available:
        raise ProvenanceReleaseError("requested contract is not bound to this manifest",
                                     "CONTRACT_MISBOUND")
    decisions = []
    for contract in origins.contracts:
        if contract["kind"] not in requested:
            continue
        if contract["kind"] == "reproducible_authorized":
            decisions.append(_reproducible_decision(origins, contract))
        else:
            decisions.append(_origin_safe_decision(origins, contract))
    return {
        "snapshot_id": origins.snapshot_id,
        "provenance_status": origins.provenance_status,
        "manifest_sha256": origins.manifest_sha256,
        "target_file": origins.target_file,
        "target_sha256": origins.target_sha256,
        "byte_reproducible": True,
        "authorization_identity_basis": "candidate_id",
        "publication_decisions": decisions,
    }


