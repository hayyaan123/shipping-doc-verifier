"""Review console: a small HTTP server over the case store, plus a read-only static export.

    python -m sdv serve --db out/cases.db [--data <bundle>]      # live: reviewers record decisions
    python -m sdv export --db out/cases.db --out site            # static snapshot for free hosting (Vercel etc.)
"""
from __future__ import annotations

import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .store import CaseStore, retry_failed

WEB = Path(__file__).parent / "web"


def render_index(snapshot: dict | None = None) -> str:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    inject = ""
    if snapshot is not None:
        # "</" is escaped so case text can never close the script tag.
        inject = "window.__SNAPSHOT__ = " + json.dumps(snapshot, ensure_ascii=False).replace("</", "<\\/") + ";"
    return html.replace("/*__SNAPSHOT__*/", inject)


def export_static(store: CaseStore, out_dir: str) -> str:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    snap = {"exported_at": time.strftime("%Y-%m-%d %H:%M"), "cases": store.list()}
    (out / "index.html").write_text(render_index(snap), encoding="utf-8")
    return str(out / "index.html")


def make_handler(store: CaseStore, inbox=None, jev=None, jev_mode: str = "auto", vision=None):
    from .runner import Runner

    runner = Runner(store, inbox, jev, jev_mode, vision) if inbox is not None else None

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, body, ctype: str = "application/json"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("content-type", f"{ctype}; charset=utf-8")
            self.send_header("content-length", str(len(data)))
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                return self._send(200, render_index(), "text/html")
            if u.path == "/api/stats":
                return self._send(200, json.dumps(store.stats()))
            if u.path == "/api/run":
                return self._send(200, json.dumps(runner.status() if runner else {"available": False}))
            if u.path == "/api/cases":
                qs = {k: v[0] for k, v in parse_qs(u.query).items()}
                rows = store.list(qs.get("status", ""), qs.get("category", ""), qs.get("q", ""), qs.get("pending") == "1")
                return self._send(200, json.dumps(rows, ensure_ascii=False))
            m = re.fullmatch(r"/api/cases/([^/]+)", u.path)
            if m:
                c = store.get(unquote(m.group(1)))
                return self._send(200 if c else 404, json.dumps(c or {"error": "not found"}, ensure_ascii=False))
            self._send(404, json.dumps({"error": "not found"}))

        def _check(self, body: dict):
            """Compare two documents a person uploaded. Body: {"files": [{"name": ..., "data": <base64>}, ...]}."""
            import base64

            from .pipeline import check_uploaded

            raw = body.get("files") or []
            if not (1 <= len(raw) <= 4) or not all(isinstance(f, dict) and f.get("name") and f.get("data") for f in raw):
                return self._send(400, json.dumps({"error": "send 1 to 4 files as {name, data(base64)}"}))
            try:
                files = [(str(f["name"])[:120], base64.b64decode(f["data"], validate=True)) for f in raw]
            except Exception:
                return self._send(400, json.dumps({"error": "file data must be base64"}))
            if any(len(d) > 15_000_000 for _, d in files):
                return self._send(413, json.dumps({"error": "file too large (15 MB max)"}))
            res = check_uploaded(files, store.next_upload_id(), jev, jev_mode, vision)
            store.upsert(res.to_dict())
            return self._send(200, json.dumps(store.get(res.email_id), ensure_ascii=False))

        def do_POST(self):
            u = urlparse(self.path)
            n = int(self.headers.get("content-length") or 0)
            if n > 70_000_000:  # a public console must not read unbounded bodies (4 files x 15 MB, base64)
                return self._send(413, json.dumps({"error": "request too large"}))
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._send(400, json.dumps({"error": "invalid JSON"}))
            if u.path == "/api/check":
                return self._check(body)
            m = re.fullmatch(r"/api/cases/([^/]+)/review", u.path)
            if m:
                try:
                    c = store.review(unquote(m.group(1)), body.get("decision", ""), body.get("note", ""), body.get("reviewer", ""))
                    return self._send(200, json.dumps(c, ensure_ascii=False))
                except KeyError:
                    return self._send(404, json.dumps({"error": "not found"}))
                except ValueError as e:
                    return self._send(400, json.dumps({"error": str(e)}))
            if u.path == "/api/run":
                if runner is None:
                    return self._send(409, json.dumps({"error": "start the server with --data to enable processing the inbox"}))
                started = runner.start(fresh=body.get("fresh", True) is not False)
                return self._send(200 if started else 409, json.dumps(runner.status() | {"started": started}))
            if u.path == "/api/retry":
                if inbox is None:
                    return self._send(409, json.dumps({"error": "start the server with --data to enable retries"}))
                return self._send(200, json.dumps(retry_failed(store, inbox, jev, jev_mode, vision)))
            self._send(404, json.dumps({"error": "not found"}))

    return H


def serve(store: CaseStore, host: str = "127.0.0.1", port: int = 8000, inbox=None, jev=None, jev_mode: str = "auto", vision=None):
    srv = ThreadingHTTPServer((host, port), make_handler(store, inbox, jev, jev_mode, vision))
    print(f"Console: http://{host}:{port}   (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
