"""Odd-PDF test: does the verifier cope with PDF layouts it was not built on?

We take emails verified clean, re-render ONE of the two documents as a PDF in an unusual layout (tables, values on
the line below, rotated pages, watermarks, multi-page, Chinese glosses, encrypted, abbreviations, ...), and check the
verdict against what the edit must cause:

  must      identical values -> OK;  one value changed -> MISMATCH on exactly that field
  safe      unfamiliar wording: never a false MISMATCH (OK or a hand-off is fine) and a changed value is never OK
  escalate  cannot be read (password protected) -> NEEDS_REVIEW / unreadable

Needs the dev-only package reportlab (pip install reportlab). Sample PDFs are written to <out>/oddpdf/ so a person can look.
"""
from __future__ import annotations

import io
import json
import random
from collections import defaultdict
from pathlib import Path

from .extract import extract_fields
from .inbox import Inbox
from .models import FIELDS
from .pipeline import process_email
from .readers import read_document
from .stress import MemInbox

BL_LABELS = {"shipper": "Shipper", "consignee": "Consignee", "notify_party": "Notify Party", "port_of_loading": "Port of Loading",
             "port_of_discharge": "Port of Discharge", "container_count": "No. of Containers", "gross_weight_kg": "Gross Weight (KGS)"}
SI_LABELS = {"shipper": "Shipper/Exporter", "consignee": "Consignee", "notify_party": "Notify Party", "port_of_loading": "Port of Loading",
             "port_of_discharge": "Discharge Port", "container_count": "Container Count", "gross_weight_kg": "Gross Weight (KG)"}
ABBREV = {"shipper": "SHPR", "consignee": "CNEE", "notify_party": "NOTIFY", "port_of_loading": "POL", "port_of_discharge": "POD",
          "container_count": "CNTR", "gross_weight_kg": "G.W."}
RENAMED = {"consignee": "Delivered To", "notify_party": "Also Advise", "port_of_discharge": "Final Destination Port",
           "container_count": "Equipment Loaded", "gross_weight_kg": "Total Cargo Weight"}
ZH = {"shipper": "发货人", "consignee": "收货人", "notify_party": "通知方", "port_of_loading": "装货港",
      "port_of_discharge": "卸货港", "container_count": "集装箱数", "gross_weight_kg": "毛重"}
NOISE = [("Vessel / Voyage", "MMSS 2507 V.257087E / 11S"), ("Booking Ref", "MSDUL0942518196"), ("HS Code", "48025600"),
         ("Description of Goods", "COATED WOODFREE PAPER IN REELS"), ("Freight", "PREPAID"), ("Marks and Numbers", "N/M")]

MUTATIONS = {
    "shipper": lambda v: "ZEBRA LOGISTICS PTE LTD",
    "consignee": lambda v: "NORTHWIND TRADING GMBH",
    "notify_party": lambda v: "HARBOUR AGENCY SDN BHD",
    "port_of_loading": lambda v: "ROTTERDAM, NETHERLANDS",
    "port_of_discharge": lambda v: "HAMBURG, GERMANY",
    "container_count": lambda v: "".join(str(int(ch) + 1) if i == 0 and ch.isdigit() else ch for i, ch in enumerate(v)) if v[:1].isdigit() else "9 x 40'HC",
    "gross_weight_kg": lambda v: "99,999 KG",
}


def _need_reportlab():
    try:
        import reportlab  # noqa: F401
    except ImportError as e:
        raise SystemExit("The odd-PDF test needs reportlab:  python -m pip install reportlab") from e


# --------------------------------------------------------------------------------------------- renderers
def _canvas(buf, size, encrypt=None):
    from reportlab.pdfgen import canvas

    return canvas.Canvas(buf, pagesize=size, encrypt=encrypt)


def _flow(c, W, H, title, rows, sep=": ", font="Helvetica", size=11, x=50, below=False, y0=None, header=None, footer=None):
    y = (y0 or H - 60)
    if title:
        c.setFont(font + ("-Bold" if font == "Helvetica" else ""), 14)
        c.drawString(x, y, title)
        y -= 34
    for label, value in rows:
        if below:
            c.setFont(font, size - 2)
            c.drawString(x, y, label)
            y -= size + 3
            c.setFont(font, size)
            c.drawString(x, y, value)
            y -= size + 12
        else:
            c.setFont(font, size)
            c.drawString(x, y, f"{label}{sep}{value}")
            y -= size + 9
        if y < 60:
            c.showPage()
            y = H - 60
    return y


def render(layout: str, title: str, rows: list, noise: list, expected_cjk: dict | None = None) -> bytes:
    """Return PDF bytes for one document in the named layout. `rows` = [(label, value)] for the 7 fields."""
    from reportlab.lib.pagesizes import A4, landscape

    W, H = A4
    buf = io.BytesIO()
    allrows = rows + noise
    if layout in ("colon_flow", "abbrev_labels", "renamed_labels"):
        c = _canvas(buf, A4); _flow(c, W, H, title, allrows)
    elif layout == "value_below":
        c = _canvas(buf, A4); _flow(c, W, H, title, allrows, below=True)
    elif layout == "no_title":
        c = _canvas(buf, A4); _flow(c, W, H, "", allrows)
    elif layout == "landscape":
        Wl, Hl = landscape(A4)
        c = _canvas(buf, (Wl, Hl)); _flow(c, Wl, Hl, title, allrows, size=10)
    elif layout == "rotated":
        c = _canvas(buf, A4); c.setPageRotation(90); _flow(c, W, H, title, allrows)
    elif layout == "blank_first_page":
        c = _canvas(buf, A4); c.showPage(); _flow(c, W, H, title, allrows)
    elif layout == "multipage":
        c = _canvas(buf, A4)
        for n, chunk in enumerate([allrows[:4], allrows[4:9], allrows[9:]], 1):
            c.setFont("Helvetica-Bold", 12); c.drawString(50, H - 40, title)
            _flow(c, W, H, "", chunk, y0=H - 90)
            c.setFont("Helvetica", 8); c.drawString(W / 2 - 30, 30, f"Page {n} of 3")
            c.showPage()
    elif layout == "watermark":
        c = _canvas(buf, A4); _flow(c, W, H, title, allrows)
        c.saveState(); c.translate(W / 2, H / 2); c.rotate(45); c.setFont("Helvetica-Bold", 64); c.setFillGray(0.82)
        c.drawCentredString(0, 0, "DRAFT - NOT NEGOTIABLE"); c.restoreState()
    elif layout == "two_column_form":
        rnd = random.Random(7)
        c = _canvas(buf, A4); c.setFont("Helvetica-Bold", 14); c.drawString(50, H - 60, title)
        y = H - 100
        for label, value in allrows:
            c.setFont("Helvetica", 9); c.drawString(50, y + rnd.uniform(-1.5, 1.5), label)
            c.setFont("Helvetica", 11); c.drawString(230, y + rnd.uniform(-1.5, 1.5), value)
            y -= 26
    elif layout == "table_grid":
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet

        doc = SimpleDocTemplate(buf, pagesize=A4)
        t = Table([[label, value] for label, value in allrows], colWidths=[150, 340])
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.6, colors.grey), ("BACKGROUND", (0, 0), (0, -1), colors.whitesmoke),
                               ("FONTSIZE", (0, 0), (-1, -1), 10), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        doc.build([Paragraph(title, getSampleStyleSheet()["Heading2"]), Spacer(1, 12), t])
        return buf.getvalue()
    elif layout == "dense":
        c = _canvas(buf, A4)
        extra = [(f"Container {i + 1}", f"GLBV31365{i:02d} 40'HC PAPER IN REELS {23000 + i * 7:,}") for i in range(14)]
        merged = allrows[:3] + extra[:7] + allrows[3:5] + extra[7:] + allrows[5:]
        _flow(c, W, H, title, merged, size=7, sep=" ")
    elif layout == "cjk_gloss":
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont

        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))  # built into reportlab: no font file needed
        c = _canvas(buf, A4)
        _flow(c, W, H, "", [(f"{label} ({expected_cjk.get(label, '')})" if expected_cjk else label, value) for label, value in rows] + noise,
              font="STSong-Light")
    elif layout == "encrypted":
        from reportlab.lib.pdfencrypt import StandardEncryption

        c = _canvas(buf, A4, StandardEncryption("secret", ownerPassword="owner")); _flow(c, W, H, title, allrows)
    elif layout == "searchable_scan":
        from PIL import Image
        from reportlab.lib.utils import ImageReader

        c = _canvas(buf, A4)
        c.drawImage(ImageReader(Image.new("L", (300, 420), 245)), 0, 0, W, H)
        t = c.beginText(50, H - 60); t.setTextRenderMode(3); t.setFont("Helvetica", 11)
        for line in [title] + [f"{a}: {b}" for a, b in allrows]:
            t.textLine(line)
        c.drawText(t)
    else:
        raise ValueError(layout)
    c.save()
    return buf.getvalue()


# layout -> (kind, label_map_name)
LAYOUTS = {
    "colon_flow": "must", "value_below": "must", "table_grid": "must", "landscape": "must", "rotated": "must",
    "multipage": "must", "blank_first_page": "must", "watermark": "must", "two_column_form": "must", "dense": "must",
    "no_title": "must", "cjk_gloss": "must", "searchable_scan": "must",
    "abbrev_labels": "safe", "renamed_labels": "safe", "encrypted": "escalate",
}
WEIGHT_FORMATS = {
    "weight 23702 KGS": lambda kg: f"{kg:.0f} KGS",
    "weight 23.702,00 KG (European)": lambda kg: f"{kg:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " KG",
    "weight 23 702 KG (space)": lambda kg: f"{kg:,.0f}".replace(",", " ") + " KG",
    "weight 23.702 MT": lambda kg: f"{kg / 1000:.3f} MT",
    "weight 23,702.00 KGS": lambda kg: f"{kg:,.2f} KGS",
}


def _values(inbox, email, which: str) -> dict:
    path = next(a for a in email["attachments"] if a.lower().endswith(f"_{which.lower()}.txt"))
    t = read_document(path.split("/")[-1], inbox.read_bytes(path))
    return {f: v.raw for f, v in extract_fields(t.lines, path, "native").items()}


def _num(s):
    import re

    m = re.search(r"[\d,]*\.?\d+", s)
    return float(m.group(0).replace(",", "")) if m else 0.0


def _rows(kind, layout, values):
    labels = SI_LABELS if kind == "SI" else BL_LABELS
    if layout == "abbrev_labels":
        labels = ABBREV
    if layout == "renamed_labels":
        labels = {**labels, **RENAMED}
    return [(labels[f], values[f]) for f in FIELDS]


def run_oddpdf(inbox: Inbox, out_dir: str = "out", limit: int = 20, jev=None, jev_mode: str = "off", keep_samples: bool = True) -> dict:
    _need_reportlab()
    mem = MemInbox(inbox)
    base = []
    for e in inbox.emails():
        a = e.get("attachments", [])
        if len(a) == 2 and all(x.lower().endswith(".txt") for x in a):
            r = process_email(e, inbox, None, "off")
            if r.category == "BL_COMPARISON" and r.status == "OK" and r.comparisons:
                base.append(e)
    base = base[:limit]

    stats = defaultdict(lambda: {"n": 0, "ok": 0, "fails": [], "outcomes": defaultdict(int)})
    samples = Path(out_dir) / "oddpdf"
    samples.mkdir(parents=True, exist_ok=True)
    cases = [(l, k, None) for l, k in LAYOUTS.items()] + [(f"colon_flow / {n}", "must", fn) for n, fn in WEIGHT_FORMATS.items()]

    for i, e in enumerate(base):
        for side in ("BL", "SI"):
            other = "SI" if side == "BL" else "BL"
            vals = _values(inbox, e, side)
            title = "BILL OF LADING (DRAFT)" if side == "BL" else "SHIPPING INSTRUCTION"
            for name, kind, wfmt in cases:
                layout = name.split(" / ")[0]
                for mutated in (None, FIELDS[(i + len(name)) % len(FIELDS)]):
                    v = dict(vals)
                    if wfmt:
                        v["gross_weight_kg"] = wfmt(_num(vals["gross_weight_kg"]))
                    if mutated:
                        v[mutated] = MUTATIONS[mutated](vals[mutated])
                        if wfmt and mutated == "gross_weight_kg":
                            v[mutated] = "99,999 KG"
                    try:
                        pdf = render(layout, title, _rows(side, layout, v), NOISE, ZH if layout == "cjk_gloss" else None)
                    except Exception as ex:  # rendering problem in our generator, not a verdict
                        raise RuntimeError(f"could not render layout {name}: {ex}") from ex
                    doc_path = next(a for a in e["attachments"] if a.lower().endswith(f"_{side.lower()}.txt"))
                    pdf_path = doc_path[:-4] + ".pdf"
                    e2 = dict(e, attachments=[pdf_path if a == doc_path else a for a in e["attachments"]])
                    mem.over = {pdf_path: pdf}
                    res = process_email(e2, mem, jev, jev_mode)
                    if kind == "must":
                        ok = res.status == "OK" if not mutated else (res.status == "MISMATCH" and res.defect_fields == [mutated])
                    elif kind == "safe":
                        ok = (res.status != "MISMATCH") if not mutated else (res.status != "OK")
                    else:
                        ok = res.status == "NEEDS_REVIEW" and res.review_reason == "unreadable"
                    key = (name, "same" if not mutated else "changed", side)
                    s = stats[key]
                    s["n"] += 1
                    s["ok"] += ok
                    s["outcomes"][f"{res.status}{'/' + res.review_reason if res.review_reason else ''}"] += 1
                    if not ok and len(s["fails"]) < 3:
                        s["fails"].append({"email_id": e["email_id"], "mutated": mutated, "got": [res.status, res.review_reason, res.defect_fields],
                                           "detail": (res.evidence.get("review_detail") or res.summary)[:160]})
                    if keep_samples and i == 0 and not mutated and side == "BL":
                        (samples / f"{name.replace(' / ', '__').replace(' ', '_').replace('(', '').replace(')', '').replace(',', '')}.pdf").write_bytes(pdf)

    rows = []
    for (name, kind, side), s in stats.items():
        rows.append({"layout": name, "edit": kind, "odd_side": side, "cases": s["n"], "passed": s["ok"], "outcomes": dict(s["outcomes"]), "failures": s["fails"]})
    total, passed = sum(r["cases"] for r in rows), sum(r["passed"] for r in rows)
    summary = {"base_emails": len(base), "cases": total, "passed": passed, "rate": round(passed / total, 4) if total else 0, "rows": rows,
               "expectation_kinds": LAYOUTS}
    Path(out_dir, "oddpdf.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def format_summary(s: dict) -> str:
    out = [f"Odd-PDF test: {s['passed']}/{s['cases']} passed ({s['rate']:.1%}) on {s['base_emails']} verified-clean emails", ""]
    agg = defaultdict(lambda: [0, 0])
    for r in s["rows"]:
        agg[(r["layout"], r["edit"])][0] += r["cases"]
        agg[(r["layout"], r["edit"])][1] += r["passed"]
    for (layout, edit), (n, ok) in agg.items():
        flag = "" if n == ok else "   <-- check"
        out.append(f"  {layout:44} {'unchanged' if edit == 'same' else 'edited':9} {ok:3}/{n:<3}{flag}")
    return "\n".join(out)
