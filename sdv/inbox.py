"""Inbox access: a folder (static bundle) or the HTTP server, same interface as the hackathon loader."""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path


class Inbox:
    def __init__(self, source: str):
        self.source = str(source).rstrip("/")
        self.is_http = self.source.startswith(("http://", "https://"))

    def _get(self, path: str) -> bytes:
        with urllib.request.urlopen(self.source + path, timeout=60) as r:
            return r.read()

    def emails(self) -> list:
        if self.is_http:
            return json.loads(self._get("/emails"))
        d = Path(self.source) / "inbox"
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob("email_*.json"))]

    def __iter__(self):
        return iter(self.emails())

    def read_bytes(self, att_path: str) -> bytes:
        if self.is_http:
            return self._get("/" + att_path.lstrip("/"))
        return (Path(self.source) / att_path).read_bytes()

    def submit(self, submission: dict) -> dict:
        if not self.is_http:
            raise RuntimeError("submit() needs the HTTP server (docker compose up --build)")
        req = urllib.request.Request(
            self.source + "/submit",
            data=json.dumps(submission).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
