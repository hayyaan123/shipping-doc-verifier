"""Per-field normalization. Each field has its own rule; there is no shared string comparison.

Everything here is deterministic. Reading is fuzzy (that is what models are for); deciding whether
two values agree is not, so it is done in plain code.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

# Values that mean "nothing was filled in".
PLACEHOLDER = re.compile(
    r"^\s*(?:n\s*/\s*a|n\.a\.?|na|tba|tbd|tbc|to\s+be\s+(?:advised|confirmed|determined|announced)|nil|none|unknown|"
    r"pending|-+|_+\s*[a-z]*|\?+.*|x{2,}|\.+)\s*$",
    re.I,
)


def is_blank(raw: Optional[str]) -> bool:
    return raw is None or not raw.strip() or bool(PLACEHOLDER.match(raw))


_LEGAL = {
    "co", "company", "ltd", "limited", "llc", "lp", "llp", "inc", "incorporated", "corp", "corporation",
    "pte", "pty", "sdn", "bhd", "gmbh", "ag", "sa", "bv", "nv", "fze", "fzco", "fz", "fzc", "jsc", "plc", "srl", "oy", "as",
}


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.upper()


def normalize_party(raw: str) -> str:
    """Case, punctuation and legal-suffix insensitive; otherwise exact.

    "KTP CO., LTD" == "KTP Co Ltd"; "FZ-LLC" == "FZ LLC". A different word is a real difference.
    """
    s = _fold(raw).replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9()]+", " ", s)  # punctuation -> space (parentheses kept: "(M)" is meaningful)
    tokens = [t for t in s.split() if t]
    # Legal-form words are dropped only where they ARE a legal suffix: at the end (a trailing "(M)"-style tag is
    # kept apart). "AS ONE TRADING" and "ONE TRADING" are different companies; "ACME CO LTD" and "ACME" are not.
    tail = []
    while tokens and tokens[-1].startswith("("):
        tail.insert(0, tokens.pop())
    body = list(tokens)
    while len(body) > 1 and body[-1].lower() in _LEGAL:
        body.pop()
    return " ".join(body + tail)


def party_name_only(lines_first: str) -> str:
    """A party value is its NAME. Addresses follow on continuation lines and are not compared."""
    return lines_first.strip()


# Port aliases: different spellings of the same place -> one canonical form.
_PORT_ALIASES = {
    "PORT KELANG": "PORT KLANG",
    "KLANG": "PORT KLANG",
    "PELABUHAN KLANG": "PORT KLANG",
    "NHAVA SHEVA": "NHAVA SHEVA",
    "JAWAHARLAL NEHRU": "NHAVA SHEVA",
    "JNPT": "NHAVA SHEVA",
    "TUTICORIN": "TUTICORIN",
    "THOOTHUKUDI": "TUTICORIN",
    "TUTICORIN THOOTHUKUDI": "TUTICORIN",
    "HO CHI MINH": "HO CHI MINH",
    "SAIGON": "HO CHI MINH",
    "BUSAN": "BUSAN",
    "PUSAN": "BUSAN",
}


def normalize_port(raw: str) -> tuple:
    """-> (city, country). UN/LOCODE and terminal names in parentheses are dropped; the NAME is compared.

    The LOCODE is deliberately not used: in this corpus a changed port keeps the original code, so
    comparing codes would miss every planted port defect.
    """
    s = _fold(raw)
    s = re.sub(r"\([^)]*\)", " ", s)  # (CNNTG), (WESTPORT)
    s = re.sub(r"\s+", " ", s).strip(" ,;-")
    parts = [p.strip() for p in s.split(",") if p.strip()]
    city = parts[0] if parts else ""
    country = parts[-1] if len(parts) > 1 else None
    city = re.sub(r"[^A-Z0-9 ]+", " ", city)
    city = re.sub(r"\s+", " ", city).strip()
    city = _PORT_ALIASES.get(city, city)
    if country:
        country = re.sub(r"[^A-Z0-9 ]+", " ", country)
        country = re.sub(r"\s+", " ", country).strip()
    return (city, country)


_INT = re.compile(r"^\s*(\d+)")


def normalize_containers(raw: str) -> Optional[int]:
    """Leading integer; the equipment type (20'GP vs 20'FCL) is not part of the count."""
    m = _INT.match(raw.replace(",", ""))
    return int(m.group(1)) if m else None


_NUM = re.compile(r"(\d[\d,]*(?:\.\d+)?)")
_TONNE = re.compile(r"\b(?:mts?|tonnes?|tons?)\b", re.I)


_GROUPED = re.compile(r"\d{1,3}(?:[ \u00a0\u202f']\d{3})+(?:[.,]\d+)?")
_PLAIN = re.compile(r"\d[\d,.]*")


def _to_number(tok: str, tonnes: bool) -> float:
    """Parse '21,577'  '21.577,00'  '21 577'  '21577.5'  '21.577' (kg) into a float, reading the separators sensibly."""
    t = re.sub(r"[ \u00a0\u202f']", "", tok).rstrip(".,")
    if "," in t and "." in t:
        dec = "," if t.rfind(",") > t.rfind(".") else "."
        thou = "." if dec == "," else ","
        return float(t.replace(thou, "").replace(dec, "."))
    if "," in t:
        if re.fullmatch(r"\d{1,3}(?:,\d{3})+", t):
            return float(t.replace(",", ""))
        if re.fullmatch(r"\d+,\d{1,2}", t):
            return float(t.replace(",", "."))
        return float(t.replace(",", ""))
    if "." in t and not tonnes and re.fullmatch(r"[1-9]\d{0,2}(?:\.\d{3})+", t):
        return float(t.replace(".", ""))  # 21.577 kg: a thousands separator (no cargo weighs 21 grams)
    return float(t)


def normalize_weight_kg(raw: str) -> Optional[float]:
    """Number with separators/units stripped; tonnes converted to kilograms."""
    m = _GROUPED.search(raw) or _PLAIN.search(raw)
    if not m:
        return None
    tonnes = bool(_TONNE.search(raw[m.end():])) or bool(re.search(r"\b(?:mts?|tonnes?|tons?)\s*[:.=]?\s*$", raw[: m.start()], re.I))
    try:
        v = _to_number(m.group(0), tonnes)
    except ValueError:
        return None
    return v * 1000.0 if tonnes else v


def normalize(field: str, raw: str):
    if field in ("shipper", "consignee", "notify_party"):
        return normalize_party(raw)
    if field in ("port_of_loading", "port_of_discharge"):
        return normalize_port(raw)
    if field == "container_count":
        return normalize_containers(raw)
    if field == "gross_weight_kg":
        return normalize_weight_kg(raw)
    return raw.strip()


def display(field: str, raw: str) -> str:
    """Human-readable value for the report: as written, minus surrounding noise."""
    return re.sub(r"\s+", " ", raw).strip()
