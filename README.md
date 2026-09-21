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
  `unreadable` > `wrong_doc_type` > `missing_attachment` > `missing_value`.
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

## Run

```bash
pip install -r requirements.txt
cp .env.example .env          # add your Jev key; .env is git-ignored
python scripts/check_jev.py   # optional: checks the key works (prints no key)

# rules only (no network)
python -m sdv run --data path/to/bundle --out out --jev off
# rules + Jev on uncertain emails (recommended)
python -m sdv run --data path/to/bundle --out out --jev auto
# add --submit to POST to the scoring server, --limit N for a quick trial
```

`--data` accepts an extracted bundle folder or the URL of the hackathon server.

## Tests

```bash
python -m pytest -q
SDV_DATA=path/to/bundle python -m pytest -q   # also runs the end-to-end data test
```

## What has and has not been validated

- On the 520-email development corpus, the rules-only pipeline reproduces the organizers'
  reference labels on every email. **That corpus is templated**, so this shows the
  pipeline handles those templates. It is not a claim about unseen real-world mail.
- The Jev integration has only been exercised against mocked HTTP responses. Run
  `scripts/check_jev.py` with a real key before relying on it, and measure `--jev auto`
  against `--jev off`.
- Scanned PDFs (no text layer) are escalated as `unreadable`; no OCR verdict is trusted.
