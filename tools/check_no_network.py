#!/usr/bin/env python3
"""Statically reject external network client usage in runtime modules."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

FORBIDDEN = [
    (re.compile(r"\bimport\s+requests\b|\bfrom\s+requests\b"), "requests client"),
    (re.compile(r"\burllib\.request\b|\bfrom\s+urllib\s+import\s+request\b"), "urllib request client"),
    (re.compile(r"\bhttp\.client\b|\bimport\s+httpx\b|\bimport\s+aiohttp\b"), "external HTTP client"),
    (re.compile(r"\bsocket\.create_connection\b|\bsmtplib\b|\bftplib\b|\bparamiko\b|\bboto3\b"), "remote socket/client"),
    (re.compile(r"https?://", re.IGNORECASE), "embedded external URL"),
]


def scan(root: Path) -> list[tuple[str, int, str, str]]:
    findings = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for pattern, label in FORBIDDEN:
                if pattern.search(line):
                    findings.append((path.relative_to(root).as_posix(), number, label, line.strip()))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default="lakemirror")
    args = parser.parse_args()
    findings = scan(Path(args.root).resolve())
    for name, number, label, line in findings:
        print(f"NETWORK_FORBIDDEN {name}:{number}: {label}: {line}")
    print(f"network scan complete; findings {len(findings)}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
