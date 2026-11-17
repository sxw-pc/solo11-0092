"""Loopback-only, read-only review console for Lake Mirror artifacts."""

from __future__ import annotations

import json
import socket
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .ledger import Ledger
from .report import build_report, render_report
from .report_html import _CSS
from .util import LakeMirrorError, pretty_json, within

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_BODY = 4096
_SECURITY = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
}
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _host_allowed(header: str) -> bool:
    if not header:
        return False
    value = header.strip().lower()
    if value.startswith("["):
        host = value[1 : value.find("]")] if "]" in value else ""
    else:
        host = value.rsplit(":", 1)[0] if value.count(":") == 1 else value
    return host in _ALLOWED_HOSTS


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>{escape(title)}</title><style>{_CSS}</style></head>
<body><header><h1>{escape(title)}</h1>
<p class="lead">Read-only local review console. Every request is served from the local SQLite ledger.</p>
<nav><a href="/">Overview</a><a href="/api/runs.json">Runs API</a><a href="/api/batches.json">Batches API</a><a href="/healthz">Health</a></nav>
</header><main>{body}</main><footer>No write, delete, upload, or remote-execution endpoint is exposed.</footer>
</body></html>"""


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(f"<th>{escape(str(item))}</th>" for item in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _dashboard(ledger: Ledger) -> str:
    runs = ledger.list_runs(limit=20)
    if not runs:
        return _page("Lake Mirror review console", "<p>No runs yet. Generate fixtures, scan, and plan first.</p>")
    latest = runs[0]
    diffs = ledger.get_diffs(latest["run_id"])
    plans = ledger.list_plans(limit=20)
    batches = ledger.list_batches(limit=20)
    run_rows = [
        [f'<a href="/report/{escape(run["run_id"])}.html">{escape(run["run_id"])}</a>', escape(run["status"]),
         escape(run["started_at"]), len(run.get("stats", {}).get("counts_by_code", {}))]
        for run in runs
    ]
    diff_rows = [
        [escape(item["severity"]), f"<code>{escape(item['code'])}</code>", escape(item["table"]),
         escape(item["partition"]), escape(item["path"]), "yes" if item["ignored"] else "no"]
        for item in diffs[:30]
    ]
    plan_rows = [
        [f"<code>{escape(item['plan_id'])}</code>", escape(item["kind"]), item["operation_count"],
         item["manual_review_count"], escape(item["scan_id"])]
        for item in plans
    ]
    batch_rows = [
        [f"<code>{escape(item['batch_id'])}</code>", escape(item["status"]),
         escape(item["backup_dir"] or "-"), escape(item["finished_at"] or "-")]
        for item in batches
    ]
    run_id = escape(latest["run_id"])
    downloads = (f'<p><a download href="/report/{run_id}.html">HTML report</a> · '
                 f'<a download href="/report/{run_id}.json">JSON report</a> · '
                 f'<a download href="/report/{run_id}.md">Markdown report</a></p>')
    body = (
        "<h2>Recent runs</h2>" + _table(["Run", "Status", "Started", "Conflict codes"], run_rows)
        + f"<h2>Latest run {run_id}</h2>{downloads}"
        + "<h3>Conflict detail (first 30)</h3>"
        + (_table(["Severity", "Code", "Table", "Partition", "Path", "Ignored"], diff_rows) if diff_rows else "<p>No conflicts.</p>")
        + "<h2>Plans</h2>" + (_table(["Plan", "Kind", "Ops", "Manual", "Scan"], plan_rows) if plan_rows else "<p>No plans.</p>")
        + "<h2>Batches</h2>" + (_table(["Batch", "Status", "Backup", "Finished"], batch_rows) if batch_rows else "<p>No batches.</p>")
    )
    return _page("Lake Mirror review console", body)


def _report(ledger: Ledger, run_id: str, fmt: str) -> str:
    return render_report(build_report(ledger, run_id), fmt)


def create_server(workdir: str | Path, host: str = "127.0.0.1", port: int = 8765,
                  db_path: str | Path | None = None) -> ThreadingHTTPServer:
    if host not in LOOPBACK_HOSTS:
        raise LakeMirrorError("server may only bind to a loopback host", "SERVER_HOST_FORBIDDEN")
    if not 0 <= int(port) <= 65535:
        raise LakeMirrorError("port must be between 0 and 65535", "SERVER_PORT_INVALID")
    root = Path(workdir).resolve()
    if not root.is_dir():
        raise LakeMirrorError(f"workdir not found: {root}", "WORKDIR_MISSING")
    database = Path(db_path).resolve() if db_path else (root / "ledger.db").resolve()
    if not within(root, database):
        raise LakeMirrorError("ledger must stay inside workdir", "SERVER_PATH_FORBIDDEN")
    Ledger(database).close()

    class Handler(BaseHTTPRequestHandler):
        server_version = "LakeMirror/1.0"
        sys_version = ""

        def log_message(self, *_: Any) -> None:
            return

        def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in _SECURITY.items():
                self.send_header(key, value)
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _reject(self, status: int, message: str, allow: str | None = None) -> None:
            body = (message + "\n").encode("utf-8")
            self._send(status, body, "text/plain; charset=utf-8", {"Allow": allow} if allow else None)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_HEAD(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._reject(405, "read-only server: write methods are not allowed", "GET, HEAD")

        do_PUT = do_POST
        do_DELETE = do_POST
        do_PATCH = do_POST

        def _dispatch(self) -> None:
            try:
                if not _host_allowed(self.headers.get("Host", "")):
                    self._reject(403, "host header is not a loopback name")
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0") or "0")
                except ValueError:
                    self._reject(400, "invalid Content-Length")
                    return
                if length > MAX_BODY:
                    self._reject(413, f"request body exceeds {MAX_BODY} bytes")
                    return
                path = urlsplit(self.path).path
                if path == "/healthz":
                    self._send(200, b'{"read_only":true,"status":"ok"}\n', "application/json; charset=utf-8")
                    return
                with Ledger(database) as ledger:
                    if path == "/":
                        self._send(200, _dashboard(ledger).encode("utf-8"), "text/html; charset=utf-8")
                    elif path == "/api/runs.json":
                        self._send(200, pretty_json(ledger.list_runs(limit=100)).encode("utf-8"), "application/json; charset=utf-8")
                    elif path == "/api/batches.json":
                        self._send(200, pretty_json(ledger.list_batches(limit=100)).encode("utf-8"), "application/json; charset=utf-8")
                    elif path.startswith("/api/run/"):
                        self._api_run(ledger, path)
                    elif path.startswith("/report/"):
                        self._report_file(ledger, path)
                    else:
                        self._reject(404, "not found")
            except (LakeMirrorError, ValueError) as exc:
                self._reject(404, f"not found: {exc}")
            except Exception:
                self._reject(500, "internal read-only server error")

        def _api_run(self, ledger: Ledger, path: str) -> None:
            run_id = path[len("/api/run/") :].removesuffix(".json")
            run = ledger.get_run(run_id)
            if run is None:
                self._reject(404, "run not found")
                return
            payload = {
                "run": run,
                "diffs": ledger.get_diffs(run_id),
                "snapshots": ledger.get_snapshots(run_id),
                "plans": [item for item in ledger.list_plans(limit=100) if item["scan_id"] == run_id],
                "batches": ledger.list_batches(limit=100),
            }
            self._send(200, pretty_json(payload).encode("utf-8"), "application/json; charset=utf-8")

        def _report_file(self, ledger: Ledger, path: str) -> None:
            name = path[len("/report/") :]
            if "." not in name:
                self._reject(404, "report format is required")
                return
            run_id, fmt = name.rsplit(".", 1)
            if fmt not in {"json", "md", "html"} or not run_id.replace("-", "").replace("_", "").replace(".", "").isalnum():
                self._reject(404, "invalid report path")
                return
            try:
                content = _report(ledger, run_id, fmt)
            except LakeMirrorError:
                self._reject(404, "run not found")
                return
            types = {"json": "application/json; charset=utf-8", "md": "text/markdown; charset=utf-8", "html": "text/html; charset=utf-8"}
            self._send(200, content.encode("utf-8"), types[fmt],
                       {"Content-Disposition": f'attachment; filename="lake-mirror-{run_id}.{fmt}"'})

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True
        if host == "::1":
            address_family = socket.AF_INET6

    return Server((host, int(port)), Handler)


def serve_forever(workdir: str | Path, host: str = "127.0.0.1", port: int = 8765,
                 db_path: str | Path | None = None) -> None:
    httpd = create_server(workdir, host, port, db_path)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
