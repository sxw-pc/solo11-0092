"""Loopback-only read-only review server tests."""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path

from lakemirror.ledger import Ledger
from lakemirror.planner import build_plan
from lakemirror.reconcile import scan_config
from lakemirror.server import MAX_BODY, _host_allowed, create_server
from lakemirror.util import LakeMirrorError, utc_now
from tests.support import make_config


def prepare(tmp: Path) -> dict:
    config = make_config(tmp)
    result = scan_config(config, "scan-server")
    plan = build_plan(config, result.scan_id, [item.as_dict() for item in result.diffs])
    now = utc_now()
    with Ledger(Path(config.workdir) / "ledger.db") as ledger:
        ledger.save_scan(result, now, now)
        ledger.save_plan(plan, now)
        ledger.create_batch("batch-server", plan["plan_id"], "running", str(Path(config.workdir) / "backups" / "batch-server"), now, {})
        ledger.finish_batch("batch-server", "completed", now, {"executed": 0})
        ledger.append("batch_finished", {"executed": 0}, now, batch_id="batch-server")
    return {"config": config, "plan": plan}


@contextmanager
def running(work: Path):
    server = create_server(work, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def request(server, method: str, path: str, headers: dict[str, str] | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    try:
        conn.request(method, path, headers=headers or {})
        response = conn.getresponse()
        body = response.read()
        heads = {key.lower(): value for key, value in response.getheaders()}
        return response.status, heads, body
    finally:
        conn.close()


class ServerTests(unittest.TestCase):
    def test_dashboard_and_json_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = prepare(Path(tmp))
            work = Path(context["config"].workdir)
            with running(work) as server:
                status, headers, body = request(server, "GET", "/")
                self.assertEqual(status, 200)
                self.assertIn("text/html", headers["content-type"])
                self.assertIn("Lake Mirror review console", body.decode())
                self.assertNotIn(b"https://", body)
                for name in ("content-security-policy", "x-content-type-options", "x-frame-options",
                             "referrer-policy", "cache-control"):
                    self.assertIn(name, headers)
                self.assertIn("default-src 'none'", headers["content-security-policy"])

                status, headers, body = request(server, "GET", "/healthz")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), {"read_only": True, "status": "ok"})
                self.assertEqual(headers["content-type"], "application/json; charset=utf-8")

                status, _headers, body = request(server, "GET", "/api/runs.json")
                self.assertEqual(status, 200)
                runs = json.loads(body)
                self.assertEqual(runs[0]["run_id"], "scan-server")

                status, _headers, body = request(server, "GET", "/api/batches.json")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)[0]["batch_id"], "batch-server")

                status, _headers, body = request(server, "GET", "/api/run/scan-server.json")
                self.assertEqual(status, 200)
                payload = json.loads(body)
                self.assertEqual(payload["run"]["run_id"], "scan-server")
                self.assertEqual(len(payload["diffs"]), 14)
                self.assertEqual(len(payload["snapshots"]), 8)
                self.assertTrue(payload["plans"][0]["plan_id"].startswith("plan-"))
                self.assertEqual(payload["batches"][0]["batch_id"], "batch-server")

    def test_all_report_formats_and_attachment_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = prepare(Path(tmp))
            work = Path(context["config"].workdir)
            with running(work) as server:
                for fmt, marker in (("json", b'"project": "lake-mirror"'), ("md", b"# Lake Mirror reconciliation report"),
                                    ("html", b"<!doctype html>")):
                    with self.subTest(fmt=fmt):
                        status, headers, body = request(server, "GET", f"/report/scan-server.{fmt}")
                        self.assertEqual(status, 200)
                        self.assertIn(marker, body)
                        self.assertTrue(headers["content-disposition"].startswith("attachment;"))
                        self.assertIn("default-src 'none'", headers["content-security-policy"])
                        if fmt == "html":
                            self.assertNotIn(b"https://", body)
                            self.assertNotIn(b"<script", body)

    def test_head_has_headers_but_no_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = prepare(Path(tmp))
            with running(Path(context["config"].workdir)) as server:
                status, headers, body = request(server, "HEAD", "/healthz")
                self.assertEqual(status, 200)
                self.assertEqual(body, b"")
                self.assertGreater(int(headers["content-length"]), 0)

    def test_write_methods_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = prepare(Path(tmp))
            with running(Path(context["config"].workdir)) as server:
                for method in ("POST", "PUT", "DELETE", "PATCH"):
                    with self.subTest(method=method):
                        status, headers, body = request(server, method, "/")
                        self.assertEqual(status, 405)
                        self.assertEqual(headers["allow"], "GET, HEAD")
                        self.assertIn(b"read-only", body)

    def test_unknown_paths_and_host_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = prepare(Path(tmp))
            with running(Path(context["config"].workdir)) as server:
                status, _headers, body = request(server, "GET", "/not-a-route")
                self.assertEqual(status, 404)
                self.assertIn(b"not found", body)
                status, _headers, _body = request(server, "GET", "/api/run/missing.json")
                self.assertEqual(status, 404)
                status, _headers, _body = request(server, "GET", "/report/missing.json")
                self.assertEqual(status, 404)
                status, _headers, body = request(server, "GET", "/", headers={"Host": "blocked.test"})
                self.assertEqual(status, 403)
                self.assertIn(b"loopback", body)

    def test_request_body_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = prepare(Path(tmp))
            with running(Path(context["config"].workdir)) as server:
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
                try:
                    conn.putrequest("GET", "/")
                    conn.putheader("Content-Length", str(MAX_BODY + 1))
                    conn.endheaders()
                    response = conn.getresponse()
                    self.assertEqual(response.status, 413)
                    self.assertIn(b"exceeds", response.read())
                finally:
                    conn.close()

    def test_construction_guards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            work.mkdir()
            with self.assertRaises(LakeMirrorError) as caught:
                create_server(work, "0.0.0.0", 8765)
            self.assertEqual(caught.exception.code, "SERVER_HOST_FORBIDDEN")
            with self.assertRaises(LakeMirrorError) as caught:
                create_server(work, "127.0.0.1", 70000)
            self.assertEqual(caught.exception.code, "SERVER_PORT_INVALID")
            with self.assertRaises(LakeMirrorError) as caught:
                create_server(work, "127.0.0.1", 8765, Path(tmp) / "outside.db")
            self.assertEqual(caught.exception.code, "SERVER_PATH_FORBIDDEN")
            with self.assertRaises(LakeMirrorError) as caught:
                create_server(Path(tmp) / "missing", "127.0.0.1", 8765)
            self.assertEqual(caught.exception.code, "WORKDIR_MISSING")

    def test_host_header_allow_list(self) -> None:
        self.assertTrue(_host_allowed("127.0.0.1"))
        self.assertTrue(_host_allowed("127.0.0.1:8765"))
        self.assertTrue(_host_allowed("localhost:8765"))
        self.assertTrue(_host_allowed("[::1]:8765"))
        self.assertFalse(_host_allowed(""))
        self.assertFalse(_host_allowed("192.0.2.1:8765"))
        self.assertFalse(_host_allowed("remote.test"))


if __name__ == "__main__":
    unittest.main()
