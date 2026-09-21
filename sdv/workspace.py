"""Batches: one inbox per batch, each with its own files and its own case database.

A batch is created empty and then given an inbox in one of three ways:
  upload  the browser sends the files of a folder (or a .zip) in chunks
  url     a link to a .zip bundle, or to an inbox API such as the hackathon server (GET /emails, GET /attachments/..)
  sample  the bundled synthetic inbox

Everything a stranger can send is treated as hostile: paths are confined to the batch folder, zip members are
checked before extraction, sizes and counts are capped, and links may not point at private network addresses
(unless the operator allows that, e.g. for a docker server on the same machine).
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Optional

from .inbox import Inbox
from .store import CaseStore

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
PROTECTED = {"sample", "uploads"}


class WorkspaceError(Exception):
    """A problem with what the user supplied (shown to them as is)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---- links --------------------------------------------------------------------------------------------------------


def check_url(url: str, allow_private: bool = False) -> str:
    p = urllib.parse.urlparse(url.strip())
    if p.scheme not in ("http", "https") or not p.hostname:
        raise WorkspaceError("The link must start with http:// or https://")
    if allow_private:
        return url.strip()
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except OSError:
        raise WorkspaceError(f"Could not find the host {p.hostname!r}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise WorkspaceError("That link points at a private network address, which this server will not contact.")
    return url.strip()


class _GuardedRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, allow_private: bool):
        self.allow_private = allow_private

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            check_url(newurl, self.allow_private)
        except WorkspaceError as e:
            raise urllib.error.URLError(f"redirect refused: {e}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def guarded_opener(allow_private: bool):
    return urllib.request.build_opener(_GuardedRedirects(allow_private))


# ---- files --------------------------------------------------------------------------------------------------------


def safe_relpath(name: str) -> Optional[str]:
    """A relative POSIX path with no '..', no absolute part and no odd segments, or None if there is nothing usable."""
    name = (name or "").replace("\\", "/").strip()
    parts = [p for p in PurePosixPath(name).parts if p not in ("", ".", "/")]
    if not parts or any(p == ".." or ":" in p or p.startswith("~") for p in parts):
        return None
    if any(p.startswith(".") for p in parts):  # .DS_Store, .git, hidden files: never part of an inbox
        return None
    return "/".join(parts)[:400]


def extract_zip(zip_path: Path, dest: Path, max_bytes: int, max_files: int) -> int:
    n = total = 0
    with zipfile.ZipFile(zip_path) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if len(infos) > max_files:
            raise WorkspaceError(f"The zip holds {len(infos)} files; the limit is {max_files}.")
        if sum(i.file_size for i in infos) > max_bytes:
            raise WorkspaceError("The zip is too large once unpacked.")
        for i in infos:
            rel = safe_relpath(i.filename)
            if rel is None or rel.startswith("__MACOSX/"):
                continue
            target = (dest / rel).resolve()
            if dest.resolve() not in target.parents:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(i) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            n += 1
            total += i.file_size
    return n


def find_bundle_root(data_dir: Path) -> Path:
    """The folder that contains inbox/email_*.json (and the attachments the emails point to)."""
    best, best_n = None, 0
    for d in [data_dir, *[p for p in data_dir.rglob("inbox") if p.is_dir()]]:
        inbox = d if d.name == "inbox" else d / "inbox"
        if not inbox.is_dir():
            continue
        n = len(list(inbox.glob("email_*.json")))
        if n > best_n:
            best, best_n = inbox.parent, n
    if best is None:
        raise WorkspaceError(
            "No emails found. Choose the folder that contains an inbox/ folder of email_*.json files and the "
            "attachments/ folder (or a .zip of it).")
    return best


# ---- workspace ----------------------------------------------------------------------------------------------------


class Workspace:
    def __init__(self, root: str, sample_dir: Optional[str] = None, max_batches: int = 30,
                 max_batch_bytes: int = 300_000_000, max_files: int = 6000, max_file_bytes: int = 40_000_000,
                 allow_private_urls: bool = False):
        self.root = Path(root)
        (self.root / "batches").mkdir(parents=True, exist_ok=True)
        self.sample_dir = Path(sample_dir) if sample_dir and Path(sample_dir).is_dir() else None
        self.max_batches, self.max_batch_bytes, self.max_files, self.max_file_bytes = max_batches, max_batch_bytes, max_files, max_file_bytes
        self.allow_private_urls = allow_private_urls
        self._lock = threading.RLock()
        self._stores: dict = {}

    # -- bookkeeping
    def _dir(self, batch_id: str) -> Path:
        if not ID_RE.match(batch_id or ""):
            raise WorkspaceError("Unknown batch.", 404)
        return self.root / "batches" / batch_id

    def exists(self, batch_id: str) -> bool:
        try:
            return (self._dir(batch_id) / "meta.json").is_file()
        except WorkspaceError:
            return False

    def meta(self, batch_id: str) -> dict:
        p = self._dir(batch_id) / "meta.json"
        if not p.is_file():
            raise WorkspaceError("Unknown batch.", 404)
        return json.loads(p.read_text(encoding="utf-8"))

    def _save_meta(self, meta: dict) -> None:
        d = self._dir(meta["id"])
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "meta.json.tmp"
        tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        os.replace(tmp, d / "meta.json")

    def data_dir(self, batch_id: str) -> Path:
        return self._dir(batch_id) / "data"

    def store(self, batch_id: str) -> CaseStore:
        self._dir(batch_id)
        with self._lock:
            if batch_id not in self._stores:
                if not self.exists(batch_id):
                    raise WorkspaceError("Unknown batch.", 404)
                self._stores[batch_id] = CaseStore(str(self._dir(batch_id) / "cases.db"))
            return self._stores[batch_id]

    def list(self) -> list:
        out = []
        for p in sorted((self.root / "batches").glob("*/meta.json")):
            try:
                m = json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                continue
            m["stats"] = self.store(m["id"]).stats()
            out.append(m)
        return sorted(out, key=lambda m: (m["id"] not in PROTECTED, -m.get("created_at", 0)))

    # -- lifecycle
    def create(self, source: dict, name: str = "", batch_id: Optional[str] = None) -> dict:
        kind = (source or {}).get("type")
        if kind not in ("upload", "url", "sample"):
            raise WorkspaceError("source.type must be upload, url or sample.")
        meta = {"id": batch_id or "b" + secrets.token_hex(4), "name": (name or "").strip()[:80], "created_at": time.time(),
                "source": {"type": kind}, "files": 0, "bytes": 0}
        if kind == "url":
            meta["source"]["url"] = check_url(str(source.get("url", "")), self.allow_private_urls)
            meta["name"] = meta["name"] or urllib.parse.urlparse(meta["source"]["url"]).netloc
        elif kind == "sample":
            if self.sample_dir is None:
                raise WorkspaceError("No sample inbox is installed on this server.", 404)
            meta["name"] = meta["name"] or "Sample inbox (synthetic)"
        else:
            meta["name"] = meta["name"] or time.strftime("Upload %d %b %H:%M")
        with self._lock:
            self._prune()
            self._save_meta(meta)
        return meta

    def ensure(self, batch_id: str, source: dict, name: str) -> dict:
        with self._lock:
            return self.meta(batch_id) if self.exists(batch_id) else self.create(source, name, batch_id)

    def _prune(self) -> None:
        metas = [m for m in (json.loads(p.read_text(encoding="utf-8")) for p in (self.root / "batches").glob("*/meta.json"))
                 if m["id"] not in PROTECTED]
        for m in sorted(metas, key=lambda m: m.get("created_at", 0))[: max(0, len(metas) - self.max_batches + 1)]:
            self.delete(m["id"])

    def delete(self, batch_id: str) -> None:
        if batch_id in PROTECTED:
            raise WorkspaceError("This batch cannot be deleted.", 403)
        d = self._dir(batch_id)
        with self._lock:
            st = self._stores.pop(batch_id, None)
            if st is not None:
                try:
                    st._db.close()
                except Exception:
                    pass
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)

    # -- files
    def add_files(self, batch_id: str, files: list) -> int:
        """files = [(relative path, bytes)]. Returns how many were stored."""
        meta = self.meta(batch_id)
        if meta["source"]["type"] != "upload":
            raise WorkspaceError("This batch does not take uploaded files.", 409)
        base = self.data_dir(batch_id)
        stored = 0
        for name, data in files:
            rel = safe_relpath(name)
            if rel is None:
                continue
            if len(data) > self.max_file_bytes:
                raise WorkspaceError(f"{rel} is larger than {self.max_file_bytes // 1_000_000} MB.", 413)
            if meta["files"] + stored + 1 > self.max_files or meta["bytes"] + len(data) > self.max_batch_bytes:
                raise WorkspaceError("This upload is larger than the limit for one batch.", 413)
            target = (base / rel).resolve()
            if base.resolve() not in target.parents:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            meta["bytes"] += len(data)
            stored += 1
        meta["files"] += stored
        self._save_meta(meta)
        return stored

    # -- the inbox a job will read
    def prepare_inbox(self, batch_id: str) -> Inbox:
        meta = self.meta(batch_id)
        src = meta["source"]
        if src["type"] == "sample":
            return Inbox(str(self.sample_dir))
        base = self.data_dir(batch_id)
        opener = guarded_opener(self.allow_private_urls)
        if src["type"] == "url":
            url = check_url(src["url"], self.allow_private_urls)
            if urllib.parse.urlparse(url).path.lower().endswith(".zip"):
                self._download_zip(url, base, opener)
            else:
                inbox = Inbox(url, opener=opener)
                try:
                    n = len(inbox.emails())
                except Exception as e:
                    raise WorkspaceError(f"Could not read an inbox from that link ({type(e).__name__}). It should be a .zip "
                                         "bundle or an inbox server that answers GET /emails.")
                if n == 0:
                    raise WorkspaceError("That inbox has no emails.")
                return inbox
        base.mkdir(parents=True, exist_ok=True)
        for z in sorted(base.rglob("*.zip")):
            dest = z.with_suffix("")
            if not dest.exists():
                extract_zip(z, dest, self.max_batch_bytes, self.max_files)
        return Inbox(str(find_bundle_root(base)))

    def _download_zip(self, url: str, base: Path, opener) -> None:
        base.mkdir(parents=True, exist_ok=True)
        target = base / "download.zip"
        if target.exists():
            return
        try:
            with opener.open(urllib.request.Request(url, headers={"User-Agent": "shipping-doc-verifier"}), timeout=60) as r, \
                    open(target, "wb") as out:
                size = 0
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.max_batch_bytes:
                        raise WorkspaceError("The file behind that link is too large.", 413)
                    out.write(chunk)
        except WorkspaceError:
            target.unlink(missing_ok=True)
            raise
        except Exception as e:
            target.unlink(missing_ok=True)
            raise WorkspaceError(f"Could not download that link ({type(e).__name__}).")
        if not zipfile.is_zipfile(target):
            target.unlink(missing_ok=True)
            raise WorkspaceError("The link did not return a zip file (a Google Drive or Dropbox share page is not a direct "
                                 "download link).")
