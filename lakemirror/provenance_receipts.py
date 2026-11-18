"""Verify source receipts and materialize locally allowed report instances."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .provenance_chain import (
    ORIGINAL,
    RECONSTRUCTED,
    ProvenanceReleaseError,
    all_combinations,
    combination_checks,
    receipt_choices,
)
from .provenance_evidence import ReportOrigins, load_report_origins
from .util import atomic_write_bytes, atomic_write_text, pretty_json, within

CONTRACT_INSTANCE = {
    "reproducible_authorized": RECONSTRUCTED,
    "origin_safe": ORIGINAL,
}


def _all_source_ids(origins: ReportOrigins) -> list[str]:
    return [candidate.id for chunk in origins.chunks for candidate in chunk.alternatives]


def _pair_evidence(origins: ReportOrigins) -> list[list[str]]:
    return [list(pair) for pair in origins.forbidden_pairs]


def verify_source_receipt(
    manifest_path: str | Path, receipt: dict[str, Any]
) -> dict[str, Any]:
    """Verify a receipt against current evidence; raise if it is not usable."""

    origins = load_report_origins(manifest_path)
    if not isinstance(receipt, dict):
        raise ProvenanceReleaseError("receipt must be an object", "RELEASE_RECEIPT_INVALID")
    kind = receipt.get("contract_kind")
    available = {contract["kind"] for contract in origins.contracts}
    if not isinstance(kind, str) or kind not in available:
        raise ProvenanceReleaseError("receipt contract is not bound", "CONTRACT_MISBOUND")
    for field, expected in (
        ("snapshot_id", origins.snapshot_id),
        ("manifest_sha256", origins.manifest_sha256),
        ("target_file", origins.target_file),
        ("target_sha256", origins.target_sha256),
    ):
        if receipt.get(field) != expected:
            code = "SNAPSHOT_MISBOUND" if field == "snapshot_id" else "CONTRACT_MISBOUND"
            raise ProvenanceReleaseError(f"receipt {field} is misbound", code)
    if receipt.get("receipt_kind") != CONTRACT_INSTANCE[kind]:
        raise ProvenanceReleaseError("receipt kind does not match contract",
                                     "RECEIPT_KIND_MISBOUND")
    if kind == "reproducible_authorized":
        return _verify_reconstructed(origins, receipt)
    return _verify_original(origins, receipt)


def _verify_reconstructed(
    origins: ReportOrigins, receipt: dict[str, Any]
) -> dict[str, Any]:
    if receipt.get("original_producer_receipt") is not False:
        raise ProvenanceReleaseError("new receipt cannot impersonate the original producer",
                                     "RECEIPT_KIND_MISBOUND")
    chosen = receipt_choices(origins, receipt)
    data, digest, hits = combination_checks(origins, chosen)
    if hits:
        raise ProvenanceReleaseError("receipt uses a forbidden pair", "FORBIDDEN_PAIR_PRESENT")
    if digest != origins.target_sha256 or data != origins.target_data:
        raise ProvenanceReleaseError("receipt chain does not reconstruct target",
                                     "RECONSTRUCTION_MISMATCH")
    if any(candidate.use_class != "external_allowed" for candidate in chosen):
        raise ProvenanceReleaseError("receipt uses a restricted source", "RESTRICTED_SOURCE")
    expected = {
        "forbidden_pairs_checked": _pair_evidence(origins),
        "forbidden_pair_hits": [],
        "reconstruction_sha256": digest,
        "byte_equal": True,
    }
    for field, value in expected.items():
        if receipt.get(field) != value:
            raise ProvenanceReleaseError(
                f"receipt {field} evidence is not reproducible", "RELEASE_RECEIPT_INVALID")
    return {
        "valid": True,
        "contract_kind": receipt["contract_kind"],
        "source_ids": [candidate.id for candidate in chosen],
        "manifest_sha256": origins.manifest_sha256,
        "target_sha256": origins.target_sha256,
        "reconstruction_sha256": digest,
        "byte_equal": True,
    }


def _verify_original(
    origins: ReportOrigins, receipt: dict[str, Any]
) -> dict[str, Any]:
    if receipt.get("original_producer_receipt") is not None:
        raise ProvenanceReleaseError("original receipt must not claim a producer record",
                                     "RECEIPT_KIND_MISBOUND")
    if receipt.get("chosen_sources") is not None:
        raise ProvenanceReleaseError(
            "original receipt must not single out one reconstruction chain",
            "RECEIPT_KIND_MISBOUND")
    checked = 0
    for choices in all_combinations(origins):
        _data, digest, hits = combination_checks(origins, choices)
        if digest != origins.target_sha256:
            continue
        checked += 1
        if hits or any(c.use_class != "external_allowed" for c in choices):
            raise ProvenanceReleaseError(
                "original receipt is not provably origin-safe", "ORIGIN_NOT_PROVABLY_SAFE")
    expected = {
        "source_ids": _all_source_ids(origins),
        "forbidden_pairs_checked": _pair_evidence(origins),
        "forbidden_pair_hits": [],
        "reconstruction_sha256": origins.target_sha256,
        "byte_equal": True,
        "combinations_checked": checked,
        "all_compatible_origins_external_allowed": True,
        "forbidden_pairs_absent_in_all_combinations": True,
    }
    for field, value in expected.items():
        if receipt.get(field) != value:
            raise ProvenanceReleaseError(
                f"receipt {field} evidence is not reproducible", "RELEASE_RECEIPT_INVALID")
    return {
        "valid": True,
        "contract_kind": receipt["contract_kind"],
        "source_ids": list(expected["source_ids"]),
        "manifest_sha256": origins.manifest_sha256,
        "target_sha256": origins.target_sha256,
        "reconstruction_sha256": origins.target_sha256,
        "byte_equal": True,
    }


def write_release_artifacts(
    manifest_path: str | Path, decision: dict[str, Any], out_dir: str | Path
) -> list[str]:
    """Write allowed report instances and their independent receipts locally."""

    origins = load_report_origins(manifest_path)
    if decision.get("manifest_sha256") != origins.manifest_sha256:
        raise ProvenanceReleaseError("decision is not bound to this manifest", "CONTRACT_MISBOUND")
    root = Path(out_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    available = {contract["kind"] for contract in origins.contracts}
    written: list[str] = []
    latest: dict[str, bytes] = {}
    prepared: list[tuple[str, bytes, str, dict[str, Any]]] = []
    items = decision.get("publication_decisions")
    if not isinstance(items, list):
        raise ProvenanceReleaseError("publication decisions missing", "RELEASE_RECEIPT_INVALID")
    for item in items:
        if not isinstance(item, dict) or item.get("status") != "allowed":
            continue
        kind = item.get("contract_kind")
        if not isinstance(kind, str) or kind not in available:
            raise ProvenanceReleaseError("decision contract is not bound", "CONTRACT_MISBOUND")
        receipt = item.get("source_receipt")
        verify_source_receipt(manifest_path, receipt)
        # The decision and its receipt must name the same contract; the
        # instance kind is derived from the bound contract, never from a claimed
        # label, so evidence cannot be laundered across contracts.
        if receipt.get("contract_kind") != kind:
            raise ProvenanceReleaseError("decision and receipt contracts differ",
                                         "CONTRACT_MISBOUND")
        expected_publish = CONTRACT_INSTANCE[kind]
        if item.get("publication_kind") != expected_publish:
            raise ProvenanceReleaseError("decision instance kind is misbound",
                                         "RECEIPT_KIND_MISBOUND")
        if kind == "reproducible_authorized":
            chosen = receipt_choices(origins, receipt)
            data = origins.header_data + b"".join(candidate.data for candidate in chosen)
            name = "report-reconstructed.txt"
        else:
            data = origins.target_data
            name = "report-original.txt"
        if latest.setdefault(name, data) != data:
            raise ProvenanceReleaseError(
                "allowed instances of the same report name differ", "RELEASE_INSTANCE_CONFLICT")
        receipt_name = f"{kind}.receipt.json"
        if not within(root, root / name) or not within(root, root / receipt_name):
            raise ProvenanceReleaseError("release artifact escapes output directory",
                                         "RELEASE_PATH_UNSAFE")
        prepared.append((name, data, receipt_name, receipt))
    # Verify every allowed decision before writing anything, so a tampered
    # decision cannot leave a partially published report behind.
    for name, data, receipt_name, receipt in prepared:
        atomic_write_bytes(root / name, data)
        atomic_write_text(root / receipt_name, pretty_json(receipt))
        for path in (str(root / name), str(root / receipt_name)):
            if path not in written:
                written.append(path)
    return written
