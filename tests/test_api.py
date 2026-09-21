"""The backend API end to end: batches, folder / zip / link intake, processing, review, and hostile input."""
import io
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sdv.api import Api, make_handler
from sdv.workspace import Workspace, extract_zip, find_bundle_root, safe_relpath, WorkspaceError

DEMO = Path(__file__).resolve().parent.parent / "demo" / "data"


def start(tmp_path, sample=False, allow_private=False, **kw):
    ws = Workspace(str(tmp_path / "ws"), sample_dir=str(DEMO) if sample else None, allow_private_urls=allow_private, **kw)
    api = Api(ws, None, "off", None)
    api.bootstrap()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return api, srv, f"http://127.0.0.1:{srv.server_address[1]}"


def call(base, method, path, body=None, raw=None, ctype="application/json"):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base + path, data=data, method=method, headers={"content-type": ctype} if data is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def multipart(files):
    """files = [(relative path, bytes)] -> (content type, body)."""
    b = uuid.uuid4().hex
    parts = [f'--{b}\r\nContent-Disposition: form-data; name="paths"\r\n\r\n{json.dumps([p for p, _ in files])}\r\n'.encode()]
    for i, (p, data) in enumerate(files):
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="f{i}.bin"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode() + data + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    return f"multipart/form-data; boundary={b}", b"".join(parts)


def upload(base, bid, files, chunk=40):
    for i in range(0, len(files), chunk):
        ctype, body = multipart(files[i:i + chunk])
        code, out = call(base, "POST", f"/api/batches/{bid}/files", raw=body, ctype=ctype)
        assert code == 200, out


def wait_done(base, bid, timeout=60):
    t = time.time()
    while time.time() - t < timeout:
        code, v = call(base, "GET", f"/api/batches/{bid}/status")
        if not v["job"]["running"]:
            return v
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def demo_files(prefix="", limit=None):
    files = [(prefix + str(p.relative_to(DEMO)), p.read_bytes()) for p in sorted(DEMO.rglob("*")) if p.is_file()]
    if limit:  # keep the first `limit` emails and their attachments
        keep = {f"email_{i:03d}" for i in range(1, limit + 1)}
        files = [(n, d) for n, d in files if any(k in n for k in keep)]
    return files


def test_sample_batch_is_ready_on_start(tmp_path):
    api, srv, base = start(tmp_path, sample=True)
    code, v = call(base, "GET", "/api/batches/sample")
    assert code == 200 and v["state"] == "done" and v["stats"]["emails"] == 520
    assert v["stats"]["by_status"] == {"OK": 154, "MISMATCH": 46, "NEEDS_REVIEW": 20}
    assert call(base, "POST", "/api/batches", {"source": {"type": "sample"}})[1]["id"] == "sample"


def test_folder_upload_in_chunks_then_process(tmp_path):
    api, srv, base = start(tmp_path)
    code, meta = call(base, "POST", "/api/batches", {"source": {"type": "upload"}, "name": "my folder"})
    assert code == 201
    bid = meta["id"]
    assert call(base, "POST", f"/api/batches/{bid}/run", {})[0] == 409  # nothing uploaded yet
    upload(base, bid, demo_files("myinbox/", limit=30), chunk=7)  # a top-level folder name in front, as a browser sends it
    code, v = call(base, "POST", f"/api/batches/{bid}/run", {})
    assert code == 200 and v["started"]
    v = wait_done(base, bid)
    assert v["state"] == "done" and v["stats"]["emails"] == 30 and not v["job"]["error"]
    rows = call(base, "GET", f"/api/batches/{bid}/cases?view=summary")[1]
    assert len(rows) == 30 and "comparisons" not in rows[0] and rows[0]["email_id"] == "email_001"
    one = next(r for r in rows if r["status"] == "MISMATCH")
    detail = call(base, "GET", f"/api/batches/{bid}/cases/{one['email_id']}")[1]
    assert detail["comparisons"] and detail["defect_fields"]
    code, rv = call(base, "POST", f"/api/batches/{bid}/cases/{one['email_id']}/review", {"decision": "confirmed_defect", "note": "checked"})
    assert code == 200 and rv["review"]["decision"] == "confirmed_defect"
    assert call(base, "POST", f"/api/batches/{bid}/cases/{one['email_id']}/review", {"decision": "bogus"})[0] == 400
    sub = call(base, "GET", f"/api/batches/{bid}/submission")[1]
    assert len(sub) == 30 and set(sub["email_001"]) == {"category", "status", "review_reason", "has_defect", "defect_fields"}


def test_zip_upload(tmp_path):
    api, srv, base = start(tmp_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, d in demo_files("bundle/", limit=12):
            z.writestr(n, d)
        z.writestr("__MACOSX/bundle/inbox/._email_001.json", b"junk")
    bid = call(base, "POST", "/api/batches", {"source": {"type": "upload"}})[1]["id"]
    upload(base, bid, [("emails.zip", buf.getvalue())])
    assert call(base, "POST", f"/api/batches/{bid}/run", {})[0] == 200
    v = wait_done(base, bid)
    assert v["state"] == "done" and v["stats"]["emails"] == 12


def test_a_folder_without_an_inbox_is_explained(tmp_path):
    api, srv, base = start(tmp_path)
    bid = call(base, "POST", "/api/batches", {"source": {"type": "upload"}})[1]["id"]
    upload(base, bid, [("photos/cat.jpg", b"x")])
    call(base, "POST", f"/api/batches/{bid}/run", {})
    v = wait_done(base, bid)
    assert v["state"] == "error" and "inbox" in v["job"]["error"]


def test_hostile_paths_and_zips_stay_inside_the_batch(tmp_path):
    for bad in ["../evil.txt", "a/../../b", "C:\\x\\y", ".git/config", "~/x", ""]:
        assert safe_relpath(bad) is None, bad
    assert safe_relpath("bundle\\inbox\\e.json") == "bundle/inbox/e.json"
    assert safe_relpath("/etc/passwd") == "etc/passwd"  # an absolute path is only ever read as relative to the batch
    api, srv, base = start(tmp_path)
    bid = call(base, "POST", "/api/batches", {"source": {"type": "upload"}})[1]["id"]
    ctype, body = multipart([("../../escape.txt", b"x"), ("ok/inbox/email_001.json", b"{}")])
    code, out = call(base, "POST", f"/api/batches/{bid}/files", raw=body, ctype=ctype)
    assert code == 200 and out["stored"] == 1
    assert not (tmp_path / "escape.txt").exists() and not (tmp_path / "ws" / "escape.txt").exists()
    zpath = tmp_path / "evil.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("../../slip.txt", b"x")
        z.writestr("good/inbox/email_001.json", b"{}")
    dest = tmp_path / "out"
    extract_zip(zpath, dest, 10_000, 10)
    assert not (tmp_path / "slip.txt").exists() and (dest / "good/inbox/email_001.json").exists()
    big = tmp_path / "bomb.zip"
    with zipfile.ZipFile(big, "w") as z:
        z.writestr("a.txt", b"0" * 5000)
    try:
        extract_zip(big, tmp_path / "o2", 1000, 10)
        raise AssertionError("expected a size refusal")
    except WorkspaceError:
        pass


def test_limits_and_bad_ids(tmp_path):
    api, srv, base = start(tmp_path, max_file_bytes=100, max_files=3)
    bid = call(base, "POST", "/api/batches", {"source": {"type": "upload"}})[1]["id"]
    ctype, body = multipart([("a.bin", b"x" * 500)])
    assert call(base, "POST", f"/api/batches/{bid}/files", raw=body, ctype=ctype)[0] == 413
    for i in range(3):
        ctype, body = multipart([(f"f{i}.txt", b"x")])
        assert call(base, "POST", f"/api/batches/{bid}/files", raw=body, ctype=ctype)[0] == 200
    ctype, body = multipart([("f9.txt", b"x")])
    assert call(base, "POST", f"/api/batches/{bid}/files", raw=body, ctype=ctype)[0] == 413
    assert call(base, "GET", "/api/batches/..%2f..%2fetc")[0] == 404
    assert call(base, "GET", "/api/batches/nope")[0] == 404
    assert call(base, "POST", "/api/batches", {"source": {"type": "carrier-pigeon"}})[0] == 400
    assert call(base, "DELETE", "/api/batches/uploads")[0] == 403


def test_links_to_private_addresses_are_refused_unless_allowed(tmp_path):
    api, srv, base = start(tmp_path)
    for url in ["http://127.0.0.1:9/x", "http://localhost/x", "http://169.254.169.254/latest", "ftp://example.com/x", "not a url"]:
        code, out = call(base, "POST", "/api/batches", {"source": {"type": "url", "url": url}})
        assert code == 400, (url, out)


class FakeInboxServer(BaseHTTPRequestHandler):
    """Speaks the hackathon server's protocol: GET /emails, GET /attachments/<file>."""
    emails = []
    files = {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/emails":
            body = json.dumps(self.emails).encode()
        elif self.path.lstrip("/") in self.files:
            body = self.files[self.path.lstrip("/")]
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_link_to_an_inbox_server(tmp_path):
    files = dict(demo_files(limit=10))
    FakeInboxServer.emails = [json.loads(v) for k, v in sorted(files.items()) if k.startswith("inbox/")]
    FakeInboxServer.files = {k: v for k, v in files.items() if k.startswith("attachments/")}
    up = ThreadingHTTPServer(("127.0.0.1", 0), FakeInboxServer)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    api, srv, base = start(tmp_path, allow_private=True)
    code, meta = call(base, "POST", "/api/batches", {"source": {"type": "url", "url": f"http://127.0.0.1:{up.server_address[1]}"}})
    assert code == 201
    assert call(base, "POST", f"/api/batches/{meta['id']}/run", {})[0] == 200
    v = wait_done(base, meta["id"])
    assert v["state"] == "done" and v["stats"]["emails"] == 10
    # a link that is not an inbox is explained, not a stack trace
    bad = call(base, "POST", "/api/batches", {"source": {"type": "url", "url": f"http://127.0.0.1:{up.server_address[1]}/nothing"}})[1]["id"]
    call(base, "POST", f"/api/batches/{bad}/run", {})
    v = wait_done(base, bad)
    assert v["state"] == "error" and "inbox" in v["job"]["error"]


def test_two_document_check_and_cors(tmp_path):
    import base64

    api, srv, base = start(tmp_path)
    si = (DEMO / "attachments/email_001_SI.txt").read_bytes()
    bl = (DEMO / "attachments/email_001_BL.txt").read_bytes().replace(b"CALLAO", b"LIMA")
    files = [{"name": n, "data": base64.b64encode(b).decode()} for n, b in (("a_SI.txt", si), ("a_BL.txt", bl))]
    code, res = call(base, "POST", "/api/batches/uploads/check", {"files": files})
    assert code == 200 and res["email_id"] == "upload_001" and res["status"] == "MISMATCH" and res["defect_fields"] == ["port_of_discharge"]
    assert call(base, "POST", "/api/batches/uploads/check", {"files": []})[0] == 400
    req = urllib.request.Request(base + "/api/batches", method="OPTIONS", headers={"origin": "https://front.example", "access-control-request-method": "POST"})
    with urllib.request.urlopen(req) as r:
        assert r.status == 204 and r.headers["access-control-allow-origin"] == "*" and "content-type" in r.headers["access-control-allow-headers"].lower()


def test_oversized_request_is_refused_and_server_keeps_working(tmp_path):
    api, srv, base = start(tmp_path)
    bid = call(base, "POST", "/api/batches", {"source": {"type": "upload"}})[1]["id"]
    req = urllib.request.Request(base + f"/api/batches/{bid}/files", data=b"x", method="POST",
                                 headers={"content-length": "80000000", "content-type": "multipart/form-data; boundary=x"})
    try:
        urllib.request.urlopen(req, timeout=5)
        raise AssertionError("expected 413")
    except (urllib.error.HTTPError, ConnectionError, OSError) as e:
        assert getattr(e, "code", 413) == 413
    assert call(base, "GET", "/api/health")[1] == {"ok": True}


def test_frontend_can_be_served_from_the_same_process(tmp_path):
    fe = tmp_path / "fe"
    fe.mkdir()
    (fe / "index.html").write_text("<h1>hi</h1>")
    (tmp_path / "secret.txt").write_text("nope")
    ws = Workspace(str(tmp_path / "ws"))
    api = Api(ws, None, "off", None, frontend_dir=str(fe))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    assert urllib.request.urlopen(base + "/").read() == b"<h1>hi</h1>"
    try:
        urllib.request.urlopen(base + "/..%2fsecret.txt")
        raise AssertionError("path escape")
    except urllib.error.HTTPError as e:
        assert e.code == 404
