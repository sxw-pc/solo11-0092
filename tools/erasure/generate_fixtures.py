#!/usr/bin/env python3
"""Regenerate the offline erasure recovery fixtures (deterministic, no network)."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MATRIX = [[1, 0, 0], [0, 1, 0], [0, 0, 1], [1, 1, 1], [1, 2, 4]]
CSV = b"order_id,customer_id,amount,dt\n10000,account-00,7.00,2024-01-01\n"
WIDTH = 22


def t2(value: int) -> int:
    shifted = value << 1
    return (shifted ^ 0x11B if shifted & 0x100 else shifted) & 0xFF


def gmul(a: int, b: int) -> int:
    result = 0
    while b:
        if b & 1:
            result ^= a
        a, b = t2(a), b >> 1
    return result


def split_file(blob: bytes) -> list[bytes]:
    padded = blob + b"\x00" * (3 * WIDTH - len(blob))
    return [padded[i * WIDTH:(i + 1) * WIDTH] for i in range(3)]


def encode_shards(data: list[bytes]) -> list[bytes]:
    return [bytes(
        gmul(row[0], data[0][j]) ^ gmul(row[1], data[1][j]) ^ gmul(row[2], data[2][j])
        for j in range(WIDTH)) for row in MATRIX]


def write_fixture(target: Path, shards: list[bytes], file_sha: str) -> None:
    target.mkdir(parents=True, exist_ok=True)
    rows = []
    for index, blob in enumerate(shards):
        if index == 2:
            rows.append({"index": 2, "file": None, "sha256": None})
            continue
        name = f"shard-{index}.bin"
        (target / name).write_bytes(blob)
        rows.append({"index": index, "file": name,
                     "sha256": hashlib.sha256(blob).hexdigest()})
    manifest = {
        "schema_version": 1, "table": "orders", "snapshot_id": "snap-erasure-0001",
        "source_file_rel": "data/dt=2024-01-01/part-erasure.csv",
        "codec": "gf256-vandermonde-3-of-5", "field_polynomial": "0x11b",
        "generator_rows": MATRIX, "original_size": len(CSV), "shard_size": WIDTH,
        "shards": rows, "contracts": ["strict_all_shards", "digest_quorum"],
        "trusted_file": "trusted-file.json"}
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    trusted = {"schema_version": 1, "table": "orders", "snapshot_id": "snap-erasure-0001",
               "source_file_rel": "data/dt=2024-01-01/part-erasure.csv",
               "file_sha256": file_sha, "original_size": len(CSV)}
    (target / "trusted-file.json").write_text(json.dumps(trusted, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    healthy = encode_shards(split_file(CSV))
    clean = ROOT / "fixtures/erasure_recovery_clean"
    if clean.exists():
        shutil.rmtree(clean)
    write_fixture(clean, healthy, hashlib.sha256(CSV).hexdigest())
    bad = ROOT / "fixtures/erasure_recovery_bad_digest"
    if bad.exists():
        shutil.rmtree(bad)
    write_fixture(bad, healthy, "0" * 64)
    print("wrote", clean.relative_to(ROOT), "and", bad.relative_to(ROOT))


if __name__ == "__main__":
    main()
