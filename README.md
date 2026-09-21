# Shipping document verifier

Averis x Monash Hackathon 2026. Reads a shipping-operations inbox, classifies every
email, and for bill-of-lading comparison requests compares the Shipping Instruction (SI)
against the draft Bill of Lading (BL) on 7 fields. Anything it cannot verify is
escalated to a human instead of guessed.

## Principles

- **AI reads, code decides.** Jev (TypeSafe) makes typed, confidence-scored judgments
  (email category, unfamiliar field labels). Code makes the final verdict.
- **Three verdicts, not two.** Every field is `match`, `mismatch` or `uncertain`. A blank
  or placeholder value is *uncertain*, never a discrepancy.
- **Evidence proportional to the claim.** Before a `MISMATCH` is reported, the field is
  re-read by a second, independent extraction path (`sdv/verify.py`). If the two reads
  disagree, the case goes to review.
- **Escalate, don't guess.** `NEEDS_REVIEW` carries a reason, in priority order:
  `unreadable` > `wrong_doc_type` > `missing_attachment` > `missing_value`. Each uncertain field carries one of
  the four: a blank, placeholder or missing value is `missing_value`; a value that is present but could not be read
  reproducibly (the second extraction path disagreed, or a scan reading is shaky) is `unreadable`. If some other field
  holds a mismatch that DID survive verification, that defect is kept (`has_defect` / `defect_fields`) on the
  `NEEDS_REVIEW` row rather than erased. A network or fetch failure is never a verdict: it is a retryable failure, and
  its submission row keeps the category the rules give it and (for a comparison request) is handed to a person as
  `NEEDS_REVIEW`, never submitted as a clean `OK`. The second extraction path reads EVERY occurrence of a label; a
  document that gives two different values for one field is not confirmed, so a wrong-line read is never an accusation.
- **Fails safe.** No Jev key, no network, or a Jev error: rules-only mode still runs.

## Layout

| Module | Job |
| --- | --- |
| `sdv/inbox.py` | Read emails and attachments from a folder or the HTTP API |
| `sdv/readers.py` | txt / pdf / docx / xlsx to text lines; no-text-layer PDFs are `unreadable` |
| `sdv/doctype.py` | Decide SI vs BL vs wrong document from **content**, not file name |
| `sdv/labels.py`, `extract.py` | Label patterns and field extraction |
| `sdv/normalize.py` | Party, port, container count and weight normalisation |
| `sdv/compare.py` | Per-field verdict; string tolerance only for OCR-provenance values, never numbers |
| `sdv/verify.py` | Second-path confirmation of mismatches |
| `sdv/jev.py` | Jev client: cached, retried, never raises, falls back to rules |
| `sdv/triage.py` | Email classification: rules first, Jev when rules are unsure |
| `sdv/pipeline.py` | Orchestration; per-email failures are captured, not fatal |
| `sdv/report.py`, `submission.py` | `results.json`, `report.txt`, `report.html`, submission JSON |
| `sdv/store.py` | SQLite case store; reviewer decisions; failed cases are retryable and distinct from verdicts |
| `sdv/console.py`, `sdv/web/` | Review console (live server) and read-only static export |
| `sdv/audit.py` | Jev vs rule-tier agreement report (validation evidence for the AI tier) |
| `sdv/stress.py` | Controlled-edit stress test (benign / defect / missing / broken / drift, plus layout / combined: a defect AND a layout quirk at once) |
| `sdv/columns.py` | The one rule, shared by both extraction paths, for where a value ends and for a value on the line below |
| `sdv/oddpdf.py` | Re-renders documents as 16 unusual PDF layouts and checks the verdicts |
| `sdv/vision.py` | Open-weights vision model / Tesseract that reads scanned PDFs as an unverified reviewer hint |
| `sdv/cloud.py` | Mirror cases and decisions to Cloud Firestore |

## Run

```bash
pip install -r requirements.txt
cp .env.example .env          # add your Jev key; .env is git-ignored
python scripts/check_jev.py   # optional: checks the key works (prints no key)

# rules only (no network)
python -m sdv run --data path/to/bundle --out out --jev off
# Jev on every email, rules as fallback (default)
python -m sdv run --data path/to/bundle --out out --jev all
# add --submit to POST to the scoring server, --limit N for a quick trial
```

`--data` accepts an extracted bundle folder or the URL of the hackathon server.

## Where Jev is used

1. **Email triage** (`sdv/triage.py`): typed `choice` for the category and a `noul` for "is the sender only
   asking for the draft to be sent?". `--jev auto` asks Jev where the rules are unsure; `--jev all` asks on every
   email and lets a confident Jev (>= 0.9) overrule the rules.
2. **Unfamiliar field labels** (`sdv/pipeline.py`): when a document uses a label the patterns do not know, Jev maps
   it to one of the 7 fields or to "none". Code then reads the value; Jev never reads or invents values.
3. **Audit and stress** (`python -m sdv audit`): asks Jev about every email and reports every disagreement with the rules.

Jev decides *what kind of thing this is*. Code decides *whether two values match*.

## Reading scanned documents (vision model)

Image-only PDFs have no text to verify, so the case is still escalated as `NEEDS_REVIEW` / `unreadable`.
With `--vision`, an open-weights vision model (or local Tesseract) transcribes the scan and the case gets an
**unverified reading** in the console ("the scan reading differs in: Consignee - may be a misread"), so the
reviewer knows where to look first. It never changes the status, reason or defect fields.

```bash
# .env:  VISION_API_KEY=hf_...   (free Hugging Face token; optional VISION_MODEL, VISION_BASE_URL)
python -m sdv run --data path/to/bundle --out out --vision auto --db out/cases.db
```

`auto` uses the vision model when a key is set and Tesseract otherwise.

Measured on the 3 scanned pairs (6 documents) with `Qwen/Qwen3-VL-30B-A3B-Instruct`: all 7 fields were extracted
from all 6 scans, with 6/6 model calls succeeding. It also misread: a thousands comma became a decimal point
(`128,544` read as `128.544`, in 2 of 3 pairs) and one port name was garbled. Local Tesseract misreads other
characters ("PTE" as "FTE"). Neither is reliable enough to decide a verdict, which is why scans are escalated and the
reading is only a hint. Hints tolerate this kind of scan noise (a separator slip or spacing is "uncertain", not a
"difference").

## Review console and case store

```bash
python -m sdv run --data path/to/bundle --out out --jev auto --db out/cases.db
python -m sdv serve --db out/cases.db --data path/to/bundle     # http://127.0.0.1:8000
```

Reviewers confirm, dismiss or clear each MISMATCH / NEEDS_REVIEW case; decisions survive a re-run.

**Check two documents.** The console's top panel takes two files (PDF, Word, Excel or text, in any order), works out
which is the Shipping Instruction and which is the draft BL from their contents, and shows the same field-by-field
verdict. Each check is saved as a case (`upload_001`, ...). Start the server with `--jev all --vision auto` so
uploaded scans also get the AI reading hint. Uploads work only in the live console, not in the static export.
`--data` on `serve` enables "Retry failed" (re-runs only cases that failed processing).

## Cloud (free tier, no credit card)

- **Case store: Cloud Firestore (Spark plan).** Setup steps are in the docstring of `sdv/cloud.py`.
  `python -m sdv sync push --db out/cases.db` uploads; `sync pull` brings back decisions made elsewhere.
- **Hosted console: Vercel Hobby.** `python -m sdv export --db out/cases.db --out site` writes a read-only
  static console; deploy the `site/` folder. Anyone with the URL can read it, so only deploy data you are
  allowed to show. `site/` is git-ignored.

Both are unverified against the organizers' definition of "cloud"; ask before the deadline.

## Running on data it was not built on

The 520-email set is a development sample. On real mail the risk is drift (new wording, layouts, formats), so every
run writes `out/health.json` and prints a `[health]` summary: escalation rate against the development baseline (9%),
Jev-vs-rules disagreements, unfamiliar field labels seen and how many could not be mapped, and processing failures.
Warnings mean "the input no longer looks like the development data: read the escalations before trusting the rest".
Model calls run in a small thread pool (4 workers) and are cached, so a re-run is cheap. When the organizers'
server is available, `--submit` returns the official score, the only real measure on unseen data.

## Odd-PDF test

`python -m sdv oddpdf --data path/to/bundle` (needs `reportlab`) re-renders one of the two documents of verified-clean
emails in 16 layouts the readers were not built on and 5 weight formats, once unchanged and once with one value edited,
on each side. Unchanged must give OK, an edited value must give MISMATCH on exactly that field, unreadable files must be
escalated, and for unfamiliar wording the rule is "never a false MISMATCH, never a silent OK for a changed value".
Sample PDFs are written to `out/oddpdf/` so they can be inspected.

First run: 382/504 (75.8%). It exposed real gaps that the 520-email set could not: rotated pages read as nonsense,
European (`21.577,00`) and space-grouped (`21 577`) weights reported as false MISMATCHes, a "Container 1 ..." table row
read as the container count, empty parentheses left by a dropped CJK font breaking label matching, and the second
extraction path not following a value on the line below its label. All were fixed generally (not per file). After the
fixes: 2,520/2,520 on 30 emails. These layouts are ones we thought of, so they are evidence of robustness, not proof.

## Tests

```bash
python -m pytest -q
SDV_DATA=path/to/bundle python -m pytest -q   # also runs the end-to-end data test
```

## Validation (measured; reproduce with the commands shown)

| Check | Command | Result |
| --- | --- | --- |
| Rules-only pipeline vs the organizers' reference labels, 520 emails | `run --jev off` + `scripts/eval_local.py` | 0 disagreements |
| Jev vs the rule tier on all 520 emails (independent second opinion) | `audit` | 520/520 agree; Jev confidence median 1.0, min 0.60 |
| Stress test: 51 verified-clean emails, 3,000+ controlled edits incl. 1,224 defect-plus-layout combinations | `stress` | benign 306/306 stay OK; defects 459/459 caught on exactly the edited field; blanks and removals 357/357 escalated; missing attachment, wrong document, corrupt PDF 153/153 escalated; unfamiliar labels 255/255 never a false mismatch (rules-only: all escalated to a person) |

| Odd-PDF test: one document re-rendered as an unusual PDF (tables, value below label, rotated, watermark, multi-page, Chinese glosses, encrypted, abbreviated / renamed labels, 5 number formats) | `oddpdf` | see "Odd-PDF test" below |

Read these with care:

- The 520-email corpus is templated. Agreement there shows the pipeline handles those templates, not unseen real mail.
- The stress edits were written by us, so they test the behaviours we thought of. They do not replace real
  variation such as new layouts, OCR noise on scans, or other languages.
- Scanned PDFs (no text layer) are escalated as `unreadable`; no OCR verdict is trusted.
- `stress --jev auto` measures how many unfamiliar labels Jev can resolve instead of escalating.
