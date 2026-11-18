"""Bounded offline erasure-shard recovery for a single orders partition file."""

from __future__ import annotations

import csv
import io
import json
from itertools import combinations
from pathlib import Path
from typing import Any

from .util import (
    LakeMirrorError,
    atomic_write_bytes,
    safe_join,
    sha256_bytes,
)

DATA_SHARDS = 3
SHARD_COUNT = 5
SUPPORTED_CODEC = "gf256-vandermonde-3-of-5"
SUPPORTED_POLYNOMIAL = "0x11b"
SUPPORTED_MATRIX = [[1, 0, 0], [0, 1, 0], [0, 0, 1], [1, 1, 1], [1, 2, 4]]
CONTRACTS = ("strict_all_shards", "digest_quorum")
ORDERS_COLUMNS = ("order_id", "customer_id", "amount", "dt")
_HEX64 = frozenset("0123456789abcdef")


class RecoveryError(LakeMirrorError):
    def __init__(self, message: str, code: str = "RECOVERY_REFUSED") -> None:
        super().__init__(message, code)


def _times_two(value: int) -> int:
    shifted = value << 1
    return (shifted ^ 0x11B if shifted & 0x100 else shifted) & 0xFF


def gmul(a: int, b: int) -> int:
    result = 0
    while b:
        if b & 1:
            result ^= a
        a, b = _times_two(a), b >> 1
    return result


def _inverse(value: int) -> int:
    if value == 0:
        raise ZeroDivisionError("zero has no GF(256) inverse")
    result = 1
    for _ in range(254):  # a^254 == a^-1 in GF(2^8)
        result = gmul(result, value)
    return result


def _invert3(matrix: list[list[int]]) -> list[list[int]] | None:
    aug = [row[:] + [1 if i == j else 0 for j in range(DATA_SHARDS)]
           for i, row in enumerate(matrix)]
    for col in range(DATA_SHARDS):
        pivot = next((r for r in range(col, DATA_SHARDS) if aug[r][col]), None)
        if pivot is None:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        aug[col] = [gmul(x, _inverse(aug[col][col])) for x in aug[col]]
        for row in range(DATA_SHARDS):
            if row != col and aug[row][col]:
                factor = aug[row][col]
                aug[row] = [x ^ gmul(factor, y) for x, y in zip(aug[row], aug[col])]
    return [row[DATA_SHARDS:] for row in aug]


def _xor_mul(acc: bytes, coefficient: int, vector: bytes) -> bytes:
    return bytes(item ^ gmul(coefficient, other) for item, other in zip(acc, vector))


def _is_hex64(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in _HEX64 for c in value)


def load_erasure_evidence(manifest_path: str | Path) -> dict[str, Any]:
    """Validate manifest, independent trusted-file authority, and each shard SHA."""

    path = Path(manifest_path).resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or manifest.get("codec") != SUPPORTED_CODEC:
        raise RecoveryError("unknown erasure input", "ERASURE_INPUT")
    if manifest.get("field_polynomial") != SUPPORTED_POLYNOMIAL:
        raise RecoveryError("unsupported field polynomial", "ERASURE_FIELD")
    if manifest.get("generator_rows") != SUPPORTED_MATRIX:
        raise RecoveryError("unsupported shard generation matrix", "ERASURE_MATRIX")
    if manifest.get("contracts") != list(CONTRACTS):
        raise RecoveryError("unknown recovery contracts", "ERASURE_CONTRACT")
    if manifest.get("table") != "orders" or not str(
            manifest.get("source_file_rel", "")).startswith("data/dt="):
        raise RecoveryError("erasure file is not an orders partition", "ERASURE_IDENTITY")
    if manifest.get("trusted_file") != "trusted-file.json":
        raise RecoveryError("trusted file identity is missing", "ERASURE_TRUST")
    trusted_bytes = safe_join(path.parent, "trusted-file.json", must_exist=True).read_bytes()
    trusted = json.loads(trusted_bytes)
    identity_keys = ("table", "snapshot_id", "source_file_rel", "original_size")
    if trusted.get("schema_version") != 1 or any(
            trusted.get(key) != manifest.get(key) for key in identity_keys):
        raise RecoveryError("trusted file identity differs", "ERASURE_TRUST")
    if not _is_hex64(trusted.get("file_sha256")):
        raise RecoveryError("trusted file digest is absent", "ERASURE_TRUST")
    width, original_size = manifest.get("shard_size"), manifest.get("original_size")
    if type(width) is not int or width <= 0 or type(original_size) is not int \
            or not 0 < original_size <= DATA_SHARDS * width:
        raise RecoveryError("invalid shard sizing", "ERASURE_SIZE")
    rows = manifest.get("shards")
    if not isinstance(rows, list) or [row.get("index") for row in rows] != list(range(SHARD_COUNT)):
        raise RecoveryError("shard indexes must be 0..4", "ERASURE_INDEX")
    present: dict[int, bytes] = {}
    records: list[dict[str, Any]] = []
    missing: list[int] = []
    for row in rows:
        index, name, recorded = row["index"], row.get("file"), row.get("sha256")
        if name is None:
            if recorded is not None:
                raise RecoveryError(f"missing shard {index} carries a digest", "ERASURE_SHA")
            missing.append(index)
            continue
        if not isinstance(name, str) or "/" in name or "\\" in name or not _is_hex64(recorded):
            raise RecoveryError("shard filename or digest is unsafe", "ERASURE_PATH")
        content = safe_join(path.parent, name, must_exist=True).read_bytes()
        if len(content) != width or sha256_bytes(content) != recorded:
            raise RecoveryError("shard byte evidence differs", "ERASURE_SHA")
        present[index] = content
        records.append({"index": index, "file": name, "sha256": recorded, "bytes": width})
    if len(present) < DATA_SHARDS:
        raise RecoveryError("fewer than three shards survive; reconstruction is impossible",
                            "ERASURE_UNDER_QUORUM")
    return {"manifest_path": path, "manifest": manifest, "trusted": trusted,
            "trusted_manifest_sha256": sha256_bytes(trusted_bytes), "present": present,
            "present_records": records, "missing": missing, "width": width,
            "original_size": original_size}


def _reconstruct(triple: tuple[int, ...], evidence: dict[str, Any]) -> tuple[bytes, list[bytes]]:
    matrix = [SUPPORTED_MATRIX[i] for i in triple]
    inverse = _invert3(matrix)
    if inverse is None:
        raise RecoveryError("singular shard selection", "ERASURE_MATRIX")
    width = evidence["width"]
    data_shards = []
    for k in range(DATA_SHARDS):
        vector = bytes(width)
        for r in range(DATA_SHARDS):
            vector = _xor_mul(vector, inverse[k][r], evidence["present"][triple[r]])
        data_shards.append(vector)
    return b"".join(data_shards)[: evidence["original_size"]], data_shards


def _encode_shard(index: int, data_shards: list[bytes], width: int) -> bytes:
    vector = bytes(width)
    for k in range(DATA_SHARDS):
        vector = _xor_mul(vector, SUPPORTED_MATRIX[index][k], data_shards[k])
    return vector


def _conflicts(data_shards: list[bytes], evidence: dict[str, Any]) -> list[dict[str, Any]]:
    findings = []
    for index, observed in evidence["present"].items():
        encoded = _encode_shard(index, data_shards, evidence["width"])
        if encoded != observed:
            offset = next(i for i in range(len(observed)) if encoded[i] != observed[i])
            findings.append({"shard_index": index, "byte_offset": offset,
                             "observed": observed[offset], "predicted": encoded[offset]})
    return findings


def _csv_columns(candidate: bytes) -> list[str] | None:
    try:
        return next(csv.reader(io.StringIO(candidate.decode("utf-8"))), None)
    except (UnicodeDecodeError, csv.Error):
        return None


def build_candidates(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    """Every distinct reconstruction reachable from three present, self-valid shards."""

    seen: dict[str, dict[str, Any]] = {}
    for triple in combinations(sorted(evidence["present"]), DATA_SHARDS):
        recovered, data_shards = _reconstruct(triple, evidence)
        digest = sha256_bytes(recovered)
        entry = seen.setdefault(digest, {
            "file_sha256": digest, "original_size": len(recovered), "source_triples": [],
            "conflicts": _conflicts(data_shards, evidence),
            "csv_columns": _csv_columns(recovered)})
        entry["source_triples"].append(list(triple))
    return [seen[key] for key in sorted(seen)]


def _evaluate(contract: str, evidence: dict[str, Any],
              candidates: list[dict[str, Any]]) -> dict[str, Any]:
    trusted_sha = evidence["trusted"]["file_sha256"]
    matches = [c for c in candidates if c["file_sha256"] == trusted_sha]
    verdict = {"contract": contract, "available_for_verification": False,
               "authorized_to_write": False, "decision": "blocked", "reason_codes": [],
               "recovered_file_sha256": None, "isolated_shards": [],
               "matching_candidate_count": len(matches), "candidate_count": len(candidates)}
    if len(matches) > 1:
        verdict["reason_codes"].append("TRUSTED_DIGEST_AMBIGUOUS")
        return verdict
    if not matches:
        verdict["reason_codes"].append("NO_CANDIDATE_MATCHES_TRUSTED_DIGEST")
        return verdict
    candidate = matches[0]
    if contract == "strict_all_shards":
        if candidate["conflicts"]:
            verdict["reason_codes"].append("VISIBLE_SHARD_CONFLICT")
            verdict["conflicts"] = candidate["conflicts"]
            return verdict
    else:
        isolated = sorted(item["shard_index"] for item in candidate["conflicts"])
        verdict["isolated_shards"] = isolated
        if len(set(evidence["present"]) - set(isolated)) < DATA_SHARDS:
            verdict["reason_codes"].append("INSUFFICIENT_CONSISTENT_SHARDS")
        if candidate["original_size"] != evidence["original_size"]:
            verdict["reason_codes"].append("ORIGINAL_SIZE_MISMATCH")
        if tuple(candidate["csv_columns"] or ()) != ORDERS_COLUMNS:
            verdict["reason_codes"].append("ORDERS_CSV_COLUMNS_MISMATCH")
        if verdict["reason_codes"]:
            return verdict
    verdict.update(available_for_verification=True, authorized_to_write=True,
                   decision="recoverable", recovered_file_sha256=trusted_sha,
                   conflicts=candidate["conflicts"])
    return verdict


def evaluate_recovery(manifest_path: str | Path) -> dict[str, Any]:
    evidence = load_erasure_evidence(manifest_path)
    candidates = build_candidates(evidence)
    manifest = evidence["manifest"]
    return {
        "table": manifest["table"], "snapshot_id": manifest["snapshot_id"],
        "source_file_rel": manifest["source_file_rel"], "codec": manifest["codec"],
        "field_polynomial": manifest["field_polynomial"], "shard_size": evidence["width"],
        "original_size": evidence["original_size"],
        "trusted_file_sha256": evidence["trusted"]["file_sha256"],
        "trusted_manifest_sha256": evidence["trusted_manifest_sha256"],
        "present_shards": evidence["present_records"], "missing_shards": evidence["missing"],
        "candidates": candidates,
        "contract_decisions": [_evaluate(name, evidence, candidates) for name in CONTRACTS]}


def _recovered_bytes(evidence: dict[str, Any], digest: str) -> bytes:
    for triple in combinations(sorted(evidence["present"]), DATA_SHARDS):
        recovered, _ = _reconstruct(triple, evidence)
        if sha256_bytes(recovered) == digest:
            return recovered
    raise RecoveryError("authorized digest disappeared during write", "RECOVERY_NONDETERMINISM")


def write_recovery(manifest_path: str | Path, output_dir: str | Path, contract: str) -> dict[str, Any]:
    if contract not in CONTRACTS:
        raise RecoveryError(f"unknown recovery contract: {contract}", "ERASURE_CONTRACT")
    evidence = load_erasure_evidence(manifest_path)
    candidates = build_candidates(evidence)
    decision = _evaluate(contract, evidence, candidates)
    target_dir = Path(output_dir).resolve()
    fixture_root = evidence["manifest_path"].parent
    try:
        target_dir.relative_to(fixture_root)
        inside_fixture = True
    except ValueError:
        inside_fixture = False
    if inside_fixture:
        raise RecoveryError("output directory must not overlap the evidence fixture",
                            "OUTPUT_PATH_UNSAFE")
    target_dir.mkdir(parents=True, exist_ok=True)
    if not target_dir.is_dir():
        raise RecoveryError("output path is not a directory", "OUTPUT_PATH_UNSAFE")
    write_info = {"contract": contract, "decision": decision["decision"],
                  "available_for_verification": decision["available_for_verification"],
                  "authorized_to_write": decision["authorized_to_write"],
                  "output_file": None, "output_sha256": None, "idempotent": False,
                  "reason_codes": decision["reason_codes"]}
    if not decision["authorized_to_write"]:
        return write_info
    output_path = safe_join(target_dir, evidence["manifest"]["source_file_rel"])
    payload = _recovered_bytes(evidence, decision["recovered_file_sha256"])
    if output_path.exists():
        if output_path.read_bytes() == payload:
            write_info.update(idempotent=True, output_file=str(output_path),
                              output_sha256=sha256_bytes(payload))
            return write_info
        raise RecoveryError(f"refusing to overwrite differing file: {output_path}",
                            "OUTPUT_CONFLICT")
    atomic_write_bytes(output_path, payload)
    if sha256_bytes(output_path.read_bytes()) != evidence["trusted"]["file_sha256"]:
        raise RecoveryError("post-write digest verification failed", "POST_WRITE_MISMATCH")
    write_info.update(output_file=str(output_path), output_sha256=sha256_bytes(payload))
    return write_info
