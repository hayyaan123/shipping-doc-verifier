"""Scan reader: an open-weights vision model (or Tesseract) that turns an image-only PDF into text lines.

This is a REVIEWER AID, not a decision maker. A scanned document is still escalated as NEEDS_REVIEW /
`unreadable`; the reading is attached to the case as an unverified hint ("the scan appears to show a different
consignee") so a person can look at the right field first. It never changes a verdict.

Backends
  vlm        any OpenAI-compatible chat endpoint that accepts images. Default: the Hugging Face router
             (https://router.huggingface.co/v1) with an open-weights model such as Qwen2.5-VL.
             .env:  VISION_API_KEY=hf_...   [VISION_BASE_URL=...]  [VISION_MODEL=...]
  tesseract  local OCR if the `tesseract` program is installed (no key, no network).
  auto       vlm when a key is set, otherwise tesseract; if the vlm call fails, tesseract.

Every failure returns None. Readings are cached on disk by document hash.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

DEFAULT_BASE_URL = "https://router.huggingface.co/v1"
DEFAULT_MODEL = "Qwen/Qwen2.5-VL-3B-Instruct"
PROMPT = ("Transcribe this scanned shipping document exactly as printed. Output one printed line per output line and keep "
          "each label together with its value on the same line, like 'Consignee: NAME'. Do not correct spelling, translate, "
          "summarise, explain or add anything. Plain text only.")


def render_pages(pdf_bytes: bytes, max_pages: int = 2, dpi: int = 200) -> list:
    """PDF -> list of PNG bytes (uses pdfplumber's renderer, so no extra system software is needed)."""
    import pdfplumber

    out = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages[:max_pages]:
            buf = io.BytesIO()
            page.to_image(resolution=dpi).original.save(buf, format="PNG")
            out.append(buf.getvalue())
    return out


class VisionReader:
    def __init__(self, backend: str = "auto", api_key: str = "", base_url: str = "", model: str = "",
                 cache_dir: str = ".cache/vision", timeout: int = 90):
        self.backend = backend
        self.api_key = api_key or ""
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or DEFAULT_MODEL
        self.cache = Path(cache_dir) if cache_dir else None
        self.timeout = timeout
        self.calls = self.cache_hits = self.failures = 0
        self.last_engine = ""
        self.last_error = ""

    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls, env_path: str = ".env", backend: str = "auto") -> "VisionReader":
        from .jev import load_env

        e = {**load_env(env_path), **{k: v for k, v in os.environ.items() if k.startswith("VISION_")}}
        return cls(backend, e.get("VISION_API_KEY", ""), e.get("VISION_BASE_URL", ""), e.get("VISION_MODEL", ""))

    def available(self) -> bool:
        return bool(self.api_key) or bool(shutil.which("tesseract"))

    # ------------------------------------------------------------------
    def read(self, pdf_bytes: bytes) -> Optional[list]:
        """Return the transcribed lines, or None if nothing usable could be produced."""
        try:
            pages = render_pages(pdf_bytes)
        except Exception:
            self.failures += 1
            return None
        order = {"vlm": ["vlm"], "tesseract": ["tesseract"], "auto": ["vlm", "tesseract"]}.get(self.backend, ["vlm", "tesseract"])
        for engine in order:
            if engine == "vlm" and not self.api_key:
                continue
            key = hashlib.sha256(pdf_bytes).hexdigest()[:24] + "-" + engine + ("-" + self.model.replace("/", "_") if engine == "vlm" else "")
            cached = self._cache_get(key)
            if cached is not None:
                self.cache_hits += 1
                self.last_engine = engine
                return cached
            text = self._vlm(pages) if engine == "vlm" else self._tesseract(pages)
            lines = [ln.rstrip() for ln in (text or "").splitlines() if ln.strip()]
            if lines:
                self._cache_put(key, lines)
                self.last_engine = engine if engine != "vlm" else f"vlm:{self.model}"
                return lines
        return None

    # ---- engines -------------------------------------------------------
    def _vlm(self, pages: list) -> Optional[str]:
        parts = [{"type": "text", "text": PROMPT}]
        for png in pages:
            parts.append({"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}})
        body = json.dumps({"model": self.model, "messages": [{"role": "user", "content": parts}],
                           "temperature": 0, "max_tokens": 1200}).encode()
        req = urllib.request.Request(self.base_url + "/chat/completions", data=body, method="POST",
                                     headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        self.calls += 1
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read())
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            self.failures += 1
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                detail = ""
            self.last_error = f"HTTP {e.code}: {detail}"
            return None
        except Exception as e:
            self.failures += 1
            self.last_error = f"{type(e).__name__}: {str(e)[:200]}"
            return None

    def _tesseract(self, pages: list) -> Optional[str]:
        exe = shutil.which("tesseract")
        if not exe:
            return None
        chunks = []
        try:
            with tempfile.TemporaryDirectory() as d:
                for i, png in enumerate(pages):
                    p = Path(d) / f"p{i}.png"
                    p.write_bytes(png)
                    r = subprocess.run([exe, str(p), "-", "--psm", "6"], capture_output=True, timeout=self.timeout)
                    chunks.append(r.stdout.decode("utf-8", "replace"))
        except Exception:
            self.failures += 1
            return None
        return "\n".join(chunks)

    # ---- cache ---------------------------------------------------------
    def _cache_get(self, key: str):
        if not self.cache:
            return None
        try:
            return json.loads((self.cache / f"{key}.json").read_text(encoding="utf-8"))
        except Exception:
            return None

    def _cache_put(self, key: str, lines: list) -> None:
        if not self.cache:
            return
        try:
            self.cache.mkdir(parents=True, exist_ok=True)
            (self.cache / f"{key}.json").write_text(json.dumps(lines, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
