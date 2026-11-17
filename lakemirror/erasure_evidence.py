"""Verify bounded offline shard input without reconstructing lake file bytes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .util import LakeMirrorError, safe_join, sha256_bytes


def inspect_erasure_manifest(manifest_path: str | Path) -> dict[str, Any]:
    path = Path(manifest_path).resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or manifest.get("codec") != "gf256-vandermonde-3-of-5":
        raise LakeMirrorError("unknown erasure input", "ERASURE_INPUT")
    if manifest.get("table") != "orders" or not str(manifest.get("source_file_rel", "")).startswith("data/dt="):
        raise LakeMirrorError("erasure file is not an orders partition", "ERASURE_IDENTITY")
    trusted_name = manifest.get("trusted_file")
    if trusted_name != "trusted-file.json":
        raise LakeMirrorError("trusted file identity is missing", "ERASURE_TRUST")
    trusted_bytes = safe_join(path.parent, trusted_name, must_exist=True).read_bytes()
    trusted = json.loads(trusted_bytes)
    if trusted.get("schema_version") != 1 or any(trusted.get(key) != manifest.get(key)
            for key in ("table", "snapshot_id", "source_file_rel", "original_size")):
        raise LakeMirrorError("trusted file identity differs", "ERASURE_TRUST")
    trusted_sha = trusted.get("file_sha256")
    if not isinstance(trusted_sha, str) or len(trusted_sha) != 64:
        raise LakeMirrorError("trusted file digest is absent", "ERASURE_TRUST")
    if manifest.get("generator_rows") != [[1,0,0],[0,1,0],[0,0,1],[1,1,1],[1,2,4]]:
        raise LakeMirrorError("unsupported shard generation matrix", "ERASURE_MATRIX")
    if manifest.get("contracts") != ["strict_all_shards", "digest_quorum"]:
        raise LakeMirrorError("unknown recovery contracts", "ERASURE_CONTRACT")
    width = manifest.get("shard_size")
    original_size = manifest.get("original_size")
    if type(width) is not int or width <= 0 or type(original_size) is not int or not 0 < original_size <= 3 * width:
        raise LakeMirrorError("invalid shard sizing", "ERASURE_SIZE")
    rows = manifest.get("shards")
    if not isinstance(rows, list) or [row.get("index") for row in rows] != list(range(5)):
        raise LakeMirrorError("shard indexes must be 0..4", "ERASURE_INDEX")
    present = []
    missing = []
    first_bytes: dict[int, int] = {}
    for row in rows:
        index = row["index"]
        name = row.get("file")
        if name is None:
            missing.append(index)
            continue
        if not isinstance(name, str) or "/" in name or "\\" in name:
            raise LakeMirrorError("shard filename is unsafe", "ERASURE_PATH")
        content = safe_join(path.parent, name, must_exist=True).read_bytes()
        if len(content) != width or sha256_bytes(content) != row.get("sha256"):
            raise LakeMirrorError("shard byte evidence differs", "ERASURE_SHA")
        present.append({"index": index, "sha256": row["sha256"], "bytes": width})
        first_bytes[index] = content[0]
    if len(present) != 4 or missing != [2]:
        raise LakeMirrorError("unexpected demonstration shard state", "ERASURE_STATE")
    # A one-byte consistency witness, not a decoded candidate file.
    def times_two(value: int) -> int:
        shifted = value << 1
        return (shifted ^ 0x11b if shifted & 0x100 else shifted) & 0xff
    missing_first = first_bytes[0] ^ first_bytes[1] ^ first_bytes[3]
    predicted_fourth = first_bytes[0] ^ times_two(first_bytes[1]) ^ times_two(times_two(missing_first))
    conflict = {"byte_offset": 0, "shard4_observed": first_bytes[4], "shard4_predicted": predicted_fourth}
    complete_path = safe_join(path.parent, f"tables/orders/{manifest['source_file_rel']}")
    return {
        "table": manifest["table"],
        "snapshot_id": manifest["snapshot_id"],
        "source_file_rel": manifest["source_file_rel"],
        "codec": manifest["codec"],
        "original_size": original_size,
        "shard_size": width,
        "file_sha256": trusted_sha,
        "trusted_manifest_sha256": sha256_bytes(trusted_bytes),
        "present_shards": present,
        "missing_shards": missing,
        "first_byte_conflict_witness": conflict,
        "complete_file_present": complete_path.is_file(),
        "contracts": manifest["contracts"],
        "recovered_bytes": None,
    }
