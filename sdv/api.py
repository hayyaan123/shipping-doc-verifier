"""The backend: a JSON API over batches. It serves no pages of its own (the frontend is a separate static site),
except as a convenience when started with --frontend.

    GET  /api/health                              liveness
    GET  /api/config                              what this server can do (limits, Jev / vision on or off)
    GET  /api/batches                             every batch with its counts
    POST /api/batches                             {"source": {"type": "upload" | "url" | "sample", "url": ...}, "name": ...}
    GET  /api/batches/{id}                        batch, counts and the running job
    DELETE /api/batches/{id}
    POST /api/batches/{id}/files                  multipart: "paths" (JSON list of relative paths) + one "file" part each
    POST /api/batches/{id}/run                    {"fresh": true}  start processing (poll .../status)
    GET  /api/batches/{id}/status
    GET  /api/batches/{id}/cases?view=summary&status=&category=&q=&pending=1
    GET  /api/batches/{id}/cases/{email_id}
    POST /api/batches/{id}/cases/{email_id}/review    {"decision": ..., "note": ...}
    POST /api/batches/{id}/retry                  re-run only cases that failed processing
    POST /api/batches/{id}/check                  {"files": [{"name", "data": base64}]}  two documents, compared directly
    GET  /api/batches/{id}/submission             submission.json (the scoring format)
    GET  /api/batches/{id}/results                every case with its evidence
"""
from __future__ import annotations

import base64
import json
import mimetypes
import re
import sys
import threading
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .runner import Runner
from .store import retry_failed
from .workspace import PROTECTED, Workspace, WorkspaceError

MAX_BODY = 60_000_000
SUMMARY_KEYS = ("email_id", "subject", "category", "category_method", "category_confidence", "status", "review_reason",
                "has_defect", "defect_fields", "error", "review")


def parse_multipart(content_type: str, body: bytes):
    """-> (fields: dict, files: [(field name, file name, bytes)])."""
    msg = BytesParser(policy=HTTP).parsebytes(b"Content-Type: " + content_type.encode("latin-1", "replace") +
                                             b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
    fields, files = {}, []
    if not msg.is_multipart():
        raise WorkspaceError("Expected a multipart upload.")
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        data = part.get_payload(decode=True) or b""
        fn = part.get_filename()
        if fn is not None:
            files.append((name, fn, data))
        else:
            fields[name] = data.decode("utf-8", "replace")
    return fields, files


class Api:
    def __init__(self, workspace: Workspace, jev=None, jev_mode: str = "off", vision=None, frontend_dir=None,
                 cors_origin: str = "*"):
        self.ws, self.jev, self.jev_mode, self.vision = workspace, jev, jev_mode, vision
        self.frontend_dir = Path(frontend_dir).resolve() if frontend_dir else None
        self.cors_origin = cors_origin
        self._runners: dict = {}
        self._lock = threading.Lock()

    def runner(self, batch_id: str) -> Runner:
        with self._lock:
            if batch_id not in self._runners:
                self._runners[batch_id] = Runner(self.ws.store(batch_id), lambda: self.ws.prepare_inbox(batch_id),
                                                 self.jev, self.jev_mode, self.vision)
            return self._runners[batch_id]

    def bootstrap(self) -> None:
        """Make sure the always-present batches exist, and that the sample has been processed once."""
        self.ws.ensure("uploads", {"type": "upload"}, "Two-document checks")
        if self.ws.sample_dir is not None:
            self.ws.ensure("sample", {"type": "sample"}, "Sample inbox (synthetic)")
            if self.ws.store("sample").stats()["emails"] == 0:
                r = self.runner("sample")
                r.start(fresh=True)
                while r.status()["running"]:
                    threading.Event().wait(0.1)

    def batch_view(self, batch_id: str) -> dict:
        meta, store, job = self.ws.meta(batch_id), self.ws.store(batch_id), self.runner(batch_id).status()
        stats = store.stats()
        if job["running"]:
            state = job["phase"]
        elif job["error"]:
            state = "error"
        elif stats["emails"]:
            state = "done"
        elif meta["files"] or meta["source"]["type"] in ("url", "sample"):
            state = "ready"
        else:
            state = "empty"
        return {"meta": meta, "stats": stats, "job": job, "state": state}


def make_handler(api: Api):
    ws = api.ws

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        # -- plumbing
        def _cors(self):
            self.send_header("access-control-allow-origin", api.cors_origin)
            self.send_header("access-control-allow-methods", "GET, POST, DELETE, OPTIONS")
            self.send_header("access-control-allow-headers", "content-type")
            self.send_header("access-control-max-age", "600")

        def _send(self, code: int, body, ctype: str = "application/json", extra: dict | None = None):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("content-type", ctype if "charset" in ctype or not ctype.startswith("text") else f"{ctype}; charset=utf-8")
            self.send_header("content-length", str(len(data)))
            self.send_header("cache-control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self._cors()
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _json(self, code: int, obj, **kw):
            return self._send(code, json.dumps(obj, ensure_ascii=False), **kw)

        def _body(self) -> bytes:
            n = int(self.headers.get("content-length") or 0)
            if n > MAX_BODY:
                raise WorkspaceError("Request too large.", 413)
            return self.rfile.read(n) if n else b""

        def _json_body(self) -> dict:
            raw = self._body()
            try:
                out = json.loads(raw or b"{}")
            except ValueError:
                raise WorkspaceError("Invalid JSON.")
            if not isinstance(out, dict):
                raise WorkspaceError("Expected a JSON object.")
            return out

        def _dispatch(self, method: str):
            u = urlparse(self.path)
            try:
                if u.path.startswith("/api/"):
                    return self._api(method, u)
                if method == "GET" and api.frontend_dir is not None:
                    return self._static(u.path)
                return self._json(404, {"error": "not found"})
            except WorkspaceError as e:
                if e.status == 413:
                    self.close_connection = True  # the unread body must not be parsed as the next request
                return self._json(e.status, {"error": str(e)})
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as e:  # never leak a traceback to the client
                print(f"[api] {method} {u.path}: {type(e).__name__}: {e}", file=sys.stderr)
                return self._json(500, {"error": "internal error"})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def do_HEAD(self):
            self._dispatch("GET")

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.send_header("content-length", "0")
            self.end_headers()

        # -- static frontend (optional)
        def _static(self, path: str):
            rel = unquote(path).lstrip("/") or "index.html"
            target = (api.frontend_dir / rel).resolve()
            if api.frontend_dir not in target.parents and target != api.frontend_dir:
                return self._json(404, {"error": "not found"})
            if target.is_dir():
                target = target / "index.html"
            if not target.is_file():
                return self._json(404, {"error": "not found"})
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            return self._send(200, target.read_bytes(), ctype)

        # -- routes
        def _api(self, method: str, u):
            path, qs = u.path, {k: v[0] for k, v in parse_qs(u.query).items()}
            if path == "/api/health" and method == "GET":
                return self._json(200, {"ok": True})
            if path == "/api/config" and method == "GET":
                return self._json(200, {"jev": api.jev is not None and api.jev_mode != "off", "jev_mode": api.jev_mode,
                                        "vision": api.vision is not None, "sample": ws.sample_dir is not None,
                                        "max_batch_mb": ws.max_batch_bytes // 1_000_000, "max_files": ws.max_files,
                                        "allow_private_urls": ws.allow_private_urls})
            if path == "/api/batches":
                if method == "GET":
                    return self._json(200, ws.list())
                if method == "POST":
                    b = self._json_body()
                    src = b.get("source") or {}
                    if src.get("type") == "sample" and ws.exists("sample"):
                        return self._json(200, ws.meta("sample"))
                    return self._json(201, ws.create(src, str(b.get("name", ""))))
            m = re.fullmatch(r"/api/batches/([^/]+)(?:/(.*))?", path)
            if not m:
                return self._json(404, {"error": "not found"})
            bid, rest = m.group(1), m.group(2) or ""
            if not ws.exists(bid):
                raise WorkspaceError("Unknown batch.", 404)
            if rest == "" and method == "GET":
                return self._json(200, api.batch_view(bid))
            if rest == "" and method == "DELETE":
                if api.runner(bid).status()["running"]:
                    raise WorkspaceError("Wait for processing to finish first.", 409)
                ws.delete(bid)
                api._runners.pop(bid, None)
                return self._json(200, {"deleted": bid})
            if rest == "status" and method == "GET":
                return self._json(200, api.batch_view(bid))
            if rest == "files" and method == "POST":
                ctype = self.headers.get("content-type", "")
                fields, files = parse_multipart(ctype, self._body())
                try:
                    paths = json.loads(fields.get("paths", "[]"))
                except ValueError:
                    raise WorkspaceError("paths must be a JSON list.")
                blobs = [f for f in files if f[0] == "file"]
                if not isinstance(paths, list) or len(paths) != len(blobs):
                    raise WorkspaceError("Send one path for each file.")
                stored = ws.add_files(bid, [(str(p), data) for p, (_, _, data) in zip(paths, blobs)])
                return self._json(200, {"stored": stored, "received": len(blobs)})
            if rest == "run" and method == "POST":
                meta = ws.meta(bid)
                if meta["source"]["type"] == "upload" and not meta["files"]:
                    raise WorkspaceError("Add the inbox files first.", 409)
                b = self._json_body()
                started = api.runner(bid).start(fresh=b.get("fresh", True) is not False)
                return self._json(200 if started else 409, dict(api.batch_view(bid), started=started))
            if rest == "cases" and method == "GET":
                rows = ws.store(bid).list(qs.get("status", ""), qs.get("category", ""), qs.get("q", ""), qs.get("pending") == "1")
                if qs.get("view") == "summary":
                    rows = [{k: r.get(k) for k in SUMMARY_KEYS} for r in rows]
                return self._json(200, rows)
            cm = re.fullmatch(r"cases/([^/]+)(/review)?", rest)
            if cm:
                eid = unquote(cm.group(1))
                if cm.group(2) is None and method == "GET":
                    c = ws.store(bid).get(eid)
                    if c is None:
                        raise WorkspaceError("Unknown case.", 404)
                    return self._json(200, c)
                if cm.group(2) and method == "POST":
                    b = self._json_body()
                    try:
                        return self._json(200, ws.store(bid).review(eid, b.get("decision", ""), str(b.get("note", ""))[:2000],
                                                                    str(b.get("reviewer", ""))[:80]))
                    except KeyError:
                        raise WorkspaceError("Unknown case.", 404)
                    except ValueError as e:
                        raise WorkspaceError(str(e))
            if rest == "retry" and method == "POST":
                store = ws.store(bid)
                return self._json(200, retry_failed(store, ws.prepare_inbox(bid), api.jev, api.jev_mode, api.vision))
            if rest == "check" and method == "POST":
                return self._check(bid, self._json_body())
            if rest == "submission" and method == "GET":
                rows = [r for r in ws.store(bid).list() if not r["email_id"].startswith("upload_")]
                sub = {r["email_id"]: {"category": r["category"], "status": r["status"], "review_reason": r.get("review_reason"),
                                       "has_defect": bool(r.get("has_defect")), "defect_fields": r.get("defect_fields") or []}
                       for r in rows}
                return self._send(200, json.dumps(sub, indent=2), extra={"content-disposition": f'attachment; filename="submission_{bid}.json"'})
            if rest == "results" and method == "GET":
                return self._send(200, json.dumps(ws.store(bid).list(), ensure_ascii=False, indent=1),
                                  extra={"content-disposition": f'attachment; filename="results_{bid}.json"'})
            return self._json(404, {"error": "not found"})

        def _check(self, bid: str, body: dict):
            from .pipeline import check_uploaded

            raw = body.get("files") or []
            if not (1 <= len(raw) <= 4) or not all(isinstance(f, dict) and f.get("name") and f.get("data") for f in raw):
                raise WorkspaceError("Send 1 to 4 files as {name, data (base64)}.")
            try:
                files = [(str(f["name"])[:120], base64.b64decode(f["data"], validate=True)) for f in raw]
            except Exception:
                raise WorkspaceError("File data must be base64.")
            if any(len(d) > 15_000_000 for _, d in files):
                raise WorkspaceError("File too large (15 MB max).", 413)
            store = ws.store(bid)
            res = check_uploaded(files, store.next_upload_id(), api.jev, api.jev_mode, api.vision)
            store.upsert(res.to_dict())
            return self._json(200, store.get(res.email_id))

    return H


def serve(api: Api, host: str = "127.0.0.1", port: int = 8000) -> None:
    api.ws.ensure("uploads", {"type": "upload"}, "Two-document checks")
    if api.ws.sample_dir is not None:
        api.ws.ensure("sample", {"type": "sample"}, "Sample inbox (synthetic)")
    # the first-time processing of the sample runs in the background so the port opens at once (hosts probe it)
    threading.Thread(target=api.bootstrap, daemon=True).start()
    srv = ThreadingHTTPServer((host, port), make_handler(api))
    srv.daemon_threads = True
    print(f"API: http://{host}:{port}/api/health" + (f"   frontend: http://{host}:{port}/" if api.frontend_dir else "") + "   (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
