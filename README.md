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
| `sdv/store.py` | SQLite case store; reviewer decisions; failed cases are retryable and distinct from verdicts |
| `sdv/console.py`, `sdv/web/` | Review console (live server) and read-only static export |
| `sdv/audit.py` | Jev vs rule-tier agreement report (validation evidence for the AI tier) |
| `sdv/stress.py` | Controlled-edit stress test (benign / defect / missing / broken / drift) |
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

## Review console and case store

```bash
python -m sdv run --data path/to/bundle --out out --jev auto --db out/cases.db
python -m sdv serve --db out/cases.db --data path/to/bundle     # http://127.0.0.1:8000
```

Reviewers confirm, dismiss or clear each MISMATCH / NEEDS_REVIEW case; decisions survive a re-run.
`--data` on `serve` enables "Retry failed" (re-runs only cases that failed processing).

## Cloud (free tier, no credit card)

- **Case store: Cloud Firestore (Spark plan).** Setup steps are in the docstring of `sdv/cloud.py`.
  `python -m sdv sync push --db out/cases.db` uploads; `sync pull` brings back decisions made elsewhere.
- **Hosted console: Vercel Hobby.** `python -m sdv export --db out/cases.db --out site` writes a read-only
  static console; deploy the `site/` folder. Anyone with the URL can read it, so only deploy data you are
  allowed to show. `site/` is git-ignored.

Both are unverified against the organizers' definition of "cloud"; ask before the deadline.

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
| Stress test: 51 verified-clean emails, 1,530 controlled edits | `stress` | benign 306/306 stay OK; defects 459/459 caught on exactly the edited field; blanks and removals 357/357 escalated; missing attachment, wrong document, corrupt PDF 153/153 escalated; unfamiliar labels 255/255 never a false mismatch (rules-only: all escalated to a person) |

Read these with care:

- The 520-email corpus is templated. Agreement there shows the pipeline handles those templates, not unseen real mail.
- The stress edits were written by us, so they test the behaviours we thought of. They do not replace real
  variation such as new layouts, OCR noise on scans, or other languages.
- Scanned PDFs (no text layer) are escalated as `unreadable`; no OCR verdict is trusted.
- With `stress --jev auto`, Jev resolved 250 of the 255 unfamiliar-label cases to the correct OK (the edit only renamed a label); the other 5 (notify party, 'Also Advise') were escalated to a person rather than guessed. Rules alone escalated all 255.
