"""Shared primitives: stable hashing, JSON, path safety, and small file helpers."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class LakeMirrorError(Exception):
    """Base error carrying a stable, machine-readable code."""

    def __init__(self, message: str, code: str = "LAKE_MIRROR_ERROR") -> None:
        super().__init__(message)
        self.code = code


class PathSecurityError(LakeMirrorError):
    """Raised whenever a path could escape an allowed root."""


CONFLICT_CODES = frozenset(
    {
        "FILE_MISSING_IN_REPLICA",
        "FILE_EXTRA_IN_REPLICA",
        "FILE_SIZE_MISMATCH",
        "FILE_SHA256_MISMATCH",
        "ROW_COUNT_MISMATCH",
        "PARTITION_MISSING",
        "PARTITION_EXTRA",
        "DUPLICATE_LOGICAL_PATH",
        "PATH_OUTSIDE_ROOT",
        "EMPTY_TABLE",
        "INVALID_PARTITION_VALUE",
        "SNAPSHOT_LAG",
        "SNAPSHOT_CHAIN_BROKEN",
        "SNAPSHOT_FORK",
        "POINTER_STALE",
        "SCHEMA_ADDITIVE_OK",
        "SCHEMA_TYPE_MISMATCH",
        "SCHEMA_FIELD_MISSING",
        "SCHEMA_FIELD_ORDER_MISMATCH",
        "SCHEMA_NULLABILITY_MISMATCH",
        "PARTITION_SPEC_MISMATCH",
        "PARTITION_COLUMN_MISSING",
    }
)

SEVERITY = {
    "FILE_MISSING_IN_REPLICA": "high",
    "FILE_EXTRA_IN_REPLICA": "medium",
    "FILE_SIZE_MISMATCH": "high",
    "FILE_SHA256_MISMATCH": "critical",
    "ROW_COUNT_MISMATCH": "high",
    "PARTITION_MISSING": "high",
    "PARTITION_EXTRA": "medium",
    "DUPLICATE_LOGICAL_PATH": "critical",
    "PATH_OUTSIDE_ROOT": "critical",
    "EMPTY_TABLE": "high",
    "INVALID_PARTITION_VALUE": "critical",
    "SNAPSHOT_LAG": "high",
    "SNAPSHOT_CHAIN_BROKEN": "critical",
    "SNAPSHOT_FORK": "critical",
    "POINTER_STALE": "medium",
    "SCHEMA_ADDITIVE_OK": "info",
    "SCHEMA_TYPE_MISMATCH": "critical",
    "SCHEMA_FIELD_MISSING": "critical",
    "SCHEMA_FIELD_ORDER_MISMATCH": "high",
    "SCHEMA_NULLABILITY_MISMATCH": "high",
    "PARTITION_SPEC_MISMATCH": "critical",
    "PARTITION_COLUMN_MISSING": "critical",
}

SIDE_PRIMARY = "primary"
SIDE_REPLICA = "replica"


def canonical_json(value: Any) -> str:
    """Return byte-stable JSON for fingerprints and audit payloads."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def pretty_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(value: Any) -> str:
    return sha256_text(canonical_json(value))


def stable_id(prefix: str, *parts: Any, length: int = 12) -> str:
    return f"{prefix}-{fingerprint(list(parts))[:length]}"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(value: str) -> datetime | None:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


_DRIVE = re.compile(r"^[A-Za-z]:")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def normalize_relpath(value: str) -> str:
    """Normalize a manifest/plan path while rejecting traversal and absolutes."""

    if not isinstance(value, str):
        raise PathSecurityError("path must be a string", code="PATH_INVALID")
    raw = value.replace("\\", "/")
    if not raw or raw.startswith("/") or _DRIVE.match(raw) or "\x00" in raw:
        raise PathSecurityError(f"unsafe path: {value!r}", code="PATH_OUTSIDE_ROOT")
    parts = raw.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise PathSecurityError(f"unsafe path: {value!r}", code="PATH_OUTSIDE_ROOT")
    if any(":" in part for part in parts):
        raise PathSecurityError(f"unsafe path: {value!r}", code="PATH_OUTSIDE_ROOT")
    return "/".join(parts)


def is_valid_sha256(value: str) -> bool:
    return bool(_SHA256.fullmatch(value.lower()))


def safe_join(root: Path, rel: str, *, must_exist: bool = False) -> Path:
    """Join a relative path and reject traversal, symlinks, and root escapes."""

    normalized = normalize_relpath(rel)
    root_path = Path(root).resolve(strict=False)
    candidate = root_path.joinpath(*normalized.split("/"))
    current = root_path
    for part in normalized.split("/"):
        current = current / part
        if current.is_symlink():
            raise PathSecurityError(
                f"symbolic link is not allowed in managed paths: {part}",
                code="SYMLINK_ESCAPE",
            )
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root_path)
    except ValueError as exc:
        raise PathSecurityError(
            f"path escapes allowed root: {rel}", code="PATH_OUTSIDE_ROOT"
        ) from exc
    if must_exist and not candidate.is_file():
        raise PathSecurityError(f"file does not exist: {rel}", code="FILE_NOT_FOUND")
    return candidate


def within(root: Path, path: Path) -> bool:
    try:
        Path(path).resolve(strict=False).relative_to(Path(root).resolve(strict=False))
        return True
    except ValueError:
        return False


def read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LakeMirrorError(f"file not found: {path}", code="FILE_NOT_FOUND") from exc
    except json.JSONDecodeError as exc:
        raise LakeMirrorError(
            f"invalid JSON at {path}:{exc.lineno}:{exc.colno}: {exc.msg}",
            code="JSON_INVALID",
        ) from exc


def write_json(path: Path, value: Any) -> None:
    atomic_write_text(Path(path), pretty_json(value))


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".lakemirror-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_copy(source: Path, target: Path) -> None:
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".lakemirror-", dir=str(target.parent))
    os.close(fd)
    try:
        with Path(source).open("rb") as src, open(temporary, "wb") as dst:
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def count_rows(path: Path) -> int:
    """Count data rows without executing any query engine."""

    path = Path(path)
    suffix = path.suffix.lower()
    try:
        if suffix == ".csv":
            with path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.reader(handle)
                next(reader, None)
                return sum(1 for row in reader if any(cell != "" for cell in row))
        if suffix == ".jsonl":
            with path.open("r", encoding="utf-8") as handle:
                return sum(1 for line in handle if line.strip())
        if suffix == ".json":
            value = json.loads(path.read_text(encoding="utf-8"))
            return len(value) if isinstance(value, list) else 1
        with path.open("r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
    except (OSError, json.JSONDecodeError, csv.Error) as exc:
        raise LakeMirrorError(f"cannot count rows for {path}: {exc}", code="ROW_COUNT_ERROR") from exc


def file_facts(path: Path) -> dict[str, Any]:
    path = Path(path)
    return {
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
        "row_count": count_rows(path),
    }
