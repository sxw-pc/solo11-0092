#!/usr/bin/env python3
"""Fail when any Python source file exceeds the project's 300-line limit."""

from __future__ import annotations

import argparse
from pathlib import Path

LIMIT = 300


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--limit", type=int, default=LIMIT)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    violations = []
    checked = 0
    for path in sorted(root.rglob("*.py")):
        if any(part in {".git", "__pycache__", ".venv", "venv"} for part in path.parts):
            continue
        checked += 1
        count = sum(1 for _ in path.open("r", encoding="utf-8", errors="replace"))
        if count > args.limit:
            violations.append((path.relative_to(root).as_posix(), count))
    for name, count in violations:
        print(f"FILE_TOO_LONG {name}: {count} > {args.limit}")
    print(f"checked {checked} Python files; limit {args.limit}; violations {len(violations)}")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
