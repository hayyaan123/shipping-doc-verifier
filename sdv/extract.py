"""Structural extraction: DocText lines -> the seven-field canonical record.

The same record shape comes out of every format, so comparison never sees a format difference.
Each field keeps `raw` next to `value`, the `label` it appeared under, and a `source` pointing at the
file and line, so a reviewer can disagree with the parse and not only with the verdict.

Labels are matched by meaning (labels.py). A label the vocabulary does not know is offered to an
optional resolver (Jev, in the pipeline); the resolver may only PICK from values the document
contains, so it cannot invent one.
"""
from __future__ import annotations

from typing import Callable, Optional

from .labels import asciify, looks_like_label, match_label
from .models import FIELDS, DocRecord, ExtractedField
from .normalize import is_blank, normalize
import re

_CONTAINER_ID = re.compile(r"\b[A-Z]{4}\d{6,7}\b")
_LEAD = re.compile(r"^[\s:\-\u2013\u2014]+")
_UNKNOWN_LABEL = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 /().,'\-]{2,60}?)\s*:\s*(\S.*)$")


def _value_after(line: str, end: int) -> str:
    return _LEAD.sub("", line[end:]).strip()


def extract_fields(
    lines: list,
    name: str,
    provenance: str = "native",
    label_resolver: Optional[Callable[[str, str], Optional[str]]] = None,
) -> dict:
    out = {f: ExtractedField(field=f, provenance=provenance) for f in FIELDS}
    unknown = []  # (line_no, label, value) candidates for the resolver

    for i, line in enumerate(lines):
        if not line.strip():
            continue
        m = match_label(line)
        if m is None:
            um = _UNKNOWN_LABEL.match(asciify(line))
            if um and label_resolver and not looks_like_label(line):
                unknown.append((i, um.group(1).strip(), line[um.end(1):].lstrip(" :").strip()))
            continue
        f, end, label = m
        if out[f].found:
            continue  # first occurrence wins (summary lines, not table rows)
        raw = _value_after(line, end)
        if f == "container_count" and _CONTAINER_ID.search(raw):
            continue  # a row of the container table ("Container 1  GLBV3136500 40'HC ..."), not the count
        if not raw:
            # Value may sit on the next line when the label line ends with only a colon/spacing.
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and lines[j][:1].isspace() and not looks_like_label(lines[j]) and lines[j].strip():
                raw = lines[j].strip()
        _fill(out[f], f, raw, label, name, i, provenance)

    # AI-assisted step: unseen labels for fields still not found.
    if label_resolver:
        for i, label, value in unknown:
            missing = [f for f in FIELDS if not out[f].found]
            if not missing:
                break
            f = label_resolver(label, value)
            if f in missing:
                _fill(out[f], f, value, label, name, i, provenance)
                out[f].confidence = min(out[f].confidence, 0.8)
    return out


def _fill(ef: ExtractedField, f: str, raw: str, label: str, name: str, i: int, provenance: str) -> None:
    ef.found = True
    ef.label = label
    ef.raw = raw
    ef.source = f"{name}:{i + 1} ({label})"
    ef.provenance = provenance
    if is_blank(raw):
        ef.blank = True
        ef.value = None
        return
    ef.value = normalize(f, raw)
    if ef.value in (None, "", ("", None)):
        ef.blank = True  # e.g. a weight line with no digits


def build_record(name: str, doc_text, doc_type: str, label_resolver=None) -> DocRecord:
    rec = DocRecord(name=name, doc_type=doc_type, readable=doc_text.readable, error=doc_text.error,
                    kind=doc_text.kind, text_layer=doc_text.text_layer)
    if doc_text.readable:
        rec.fields = extract_fields(doc_text.lines, name, "native", label_resolver)
    return rec
