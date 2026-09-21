"""Startup dependency check: a missing library is an environment problem, never a verdict about a document."""
from __future__ import annotations

import importlib

REQUIRED = {"pdfplumber": "pdfplumber", "docx": "python-docx", "openpyxl": "openpyxl"}


class MissingDependency(RuntimeError):
    pass


def missing() -> list:
    out = []
    for mod, pkg in REQUIRED.items():
        try:
            importlib.import_module(mod)
        except ImportError:
            out.append(pkg)
    return out


def require() -> None:
    m = missing()
    if m:
        raise MissingDependency(
            f"Missing Python packages: {', '.join(m)}.\n"
            f"Install them with:  python -m pip install -r requirements.txt\n"
            f"(Without them PDF/Word/Excel attachments cannot be read, and results would be wrong.)")
