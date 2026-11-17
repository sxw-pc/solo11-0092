"""Static guard-tool tests and repository-wide policy checks."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_tool(name: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "tools" / name), *args],
        cwd=str(ROOT), text=True, capture_output=True, timeout=60,
    )


class FileLengthToolTests(unittest.TestCase):
    def test_flags_long_files_and_honors_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "short.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
            (root / "long.py").write_text("\n".join(f"v{i} = {i}" for i in range(305)) + "\n", encoding="utf-8")
            failed = run_tool("check_file_length.py", str(root))
            self.assertEqual(failed.returncode, 1)
            self.assertIn("FILE_TOO_LONG long.py: 305 > 300", failed.stdout)
            passed = run_tool("check_file_length.py", str(root), "--limit", "400")
            self.assertEqual(passed.returncode, 0)
            self.assertIn("violations 0", passed.stdout)

    def test_skips_pycache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "__pycache__"
            cache.mkdir()
            (cache / "generated.py").write_text("\n" * 400, encoding="utf-8")
            result = run_tool("check_file_length.py", str(root))
            self.assertEqual(result.returncode, 0)
            self.assertIn("checked 0 Python files", result.stdout)


class NetworkGuardToolTests(unittest.TestCase):
    def test_flags_clients_and_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bad.py").write_text(
                "import requests\n"
                "import http.client\n"
                "endpoint = 'https://example.test/collect'\n",
                encoding="utf-8",
            )
            result = run_tool("check_no_network.py", str(root))
            self.assertEqual(result.returncode, 1)
            self.assertIn("requests client", result.stdout)
            self.assertIn("external HTTP client", result.stdout)
            self.assertIn("embedded external URL", result.stdout)

    def test_allows_offline_standard_library(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "good.py").write_text(
                "import json, sqlite3, socket\n"
                "from http.server import BaseHTTPRequestHandler\n"
                "def local(host):\n"
                "    return host in {'127.0.0.1', 'localhost', '::1'}\n",
                encoding="utf-8",
            )
            result = run_tool("check_no_network.py", str(root))
            self.assertEqual(result.returncode, 0)
            self.assertIn("findings 0", result.stdout)


class RepositoryPolicyTests(unittest.TestCase):
    def test_runtime_and_tests_respect_file_length_limit(self) -> None:
        result = run_tool("check_file_length.py", ".")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("violations 0", result.stdout)

    def test_runtime_has_no_network_clients(self) -> None:
        result = run_tool("check_no_network.py", "lakemirror")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("findings 0", result.stdout)


if __name__ == "__main__":
    unittest.main()
