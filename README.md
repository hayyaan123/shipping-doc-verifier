# Shipping document verifier

Averis x Monash Hackathon 2026. Reads a shipping-operations inbox, classifies every
email, and for bill-of-lading comparison requests compares the Shipping Instruction (SI)
against the draft Bill of Lading (BL) on 7 fields. Anything it cannot verify is
escalated to a human instead of guessed.

**Live demo: https://shipping-doc-verifier.onrender.com** — the bundled synthetic inbox (520 emails) is already
processed there; the home page also takes a folder, a `.zip` or a link of your own. It needs no API key: the Jev
answers it shows were computed ahead of time and ship in `demo/jev_cache/`. On a free host the first request after
15 idle minutes takes about a minute to wake.

## Quick start (setup)

```bash
git clone https://github.com/hayyaan123/shipping-doc-verifier && cd shipping-doc-verifier
pip install -r requirements.txt                    # Python 3.10+
python -m sdv serve --workdir workspace --sample demo/data --frontend frontend
# open http://127.0.0.1:8000  ->  drop a folder, a .zip or a link on the home page
```

The app is two separate parts: a **backend** (`sdv/`, a JSON API) and a **frontend** (`frontend/`, plain static
files that only talk to that API). `--frontend frontend` is a convenience that lets the backend also serve the static
files; they can equally be hosted apart (see "Architecture and hosting").
Or with Docker: `docker compose up --build` and open http://localhost:7860.
A live copy runs on a free cloud host: https://shipping-doc-verifier.onrender.com (see "Hosting").
Jev is optional: put `TYPESAFE_API_KEY=...` in `.env` (copy `.env.example`) and add `--jev all` to turn it on.
The command-line pipeline still exists for batch runs: `python -m sdv run --data demo/data --out out --jev off`.

## Using it

On the home page choose where the emails come from. Each source becomes a **batch** with its own page, progress bar,
case list, reviewer decisions and downloadable `submission.json`.

- **A folder** (drag it in, or "Choose folder"): the bundle layout, i.e. a folder that contains `inbox/email_*.json`
  and `attachments/`. The browser sends the files in small chunks with their relative paths.
- **A .zip** of that folder.
- **A link**: either a `.zip` URL, or the base URL of a server that answers `GET /emails` (the hackathon's docker
  server). Addresses on private networks are refused (SSRF guard) unless the backend runs with `--allow-private-urls`,
  which is what you want when the organizers' docker is on the same machine.
- **The bundled sample** (520 synthetic emails), one click.
- **Check two documents**: drop an SI and a draft BL (any order, PDF / Word / Excel / text); the verdict appears at once.

A folder without an `inbox/` is refused with an explanation, never silently processed as "0 emails". Processing runs
in the background (so the page shows live progress), each email is saved as it finishes, a failed email is a retryable
failure (never a verdict), and "Process again" re-runs a batch. Reviewers confirm, dismiss or clear each MISMATCH /
NEEDS_REVIEW case; decisions survive a re-run. Uploads are size- and count-limited, zip paths are sanitised (no
zip-slip, no zip bombs), and at most 30 batches are kept (oldest pruned).

## How it works

```
email (subject, body, attachments)
  -> triage: rules first, Jev (typed decision + confidence) where they are unsure -> 5 categories
  -> if a comparison request with 2 attachments:
       read PDF / Word / Excel / text  -> lines
       decide SI vs BL from the CONTENT, not the file name
       extract 7 fields (label patterns; unfamiliar labels are mapped by Jev, which may only PICK a field)
       compare per field: match / mismatch / uncertain
       every claimed mismatch is re-read by a second, independent extraction path
       anything unverifiable -> NEEDS_REVIEW with one of 4 reasons, for a person to decide
  -> submission JSON + per-batch case store + review UI
```

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
| `sdv/api.py` | Backend JSON API (batches, files, run, cases, review, submission, two-document check), CORS, optional static frontend |
| `sdv/workspace.py` | Batches on disk: folder / zip / link intake, path and zip safety, SSRF guard, limits |
| `sdv/runner.py` | Background job per batch (preparing, processing, done / error) with live progress |
| `frontend/` | Separate static frontend (plain HTML / CSS / JS); `config.js` says where the API lives |
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
**unverified reading** in the case view ("the scan reading differs in: Consignee - may be a misread"), so the
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

## API

All under `/api`; JSON in and out. `{id}` is a batch id, `{eid}` an email id.

| Route | Purpose |
| --- | --- |
| `GET /health`, `GET /config` | liveness; server limits and modes |
| `GET /batches`, `POST /batches` | list; create (`{"name", "source": {"type": "upload" or "url" or "sample", "url": ...}}`) |
| `GET /batches/{id}`, `DELETE /batches/{id}` | batch, its stats and job state; delete |
| `POST /batches/{id}/files` | multipart upload (files + a `paths` JSON field with relative paths), or a `.zip` |
| `POST /batches/{id}/run`, `/retry` | process (again); re-run only failed cases |
| `GET /batches/{id}/status` | job phase and progress |
| `GET /batches/{id}/cases?view=summary&status=&category=&q=&pending=` | filtered list |
| `GET /batches/{id}/cases/{eid}`, `POST .../review` | one case in full; confirm / dismiss / clear |
| `GET /batches/{id}/submission`, `/results` | the submission JSON; the full results |
| `POST /batches/{id}/check` | two documents (base64) checked at once and saved as a case |

Each batch keeps its own SQLite case database (`CaseStore`) next to its uploaded files in `--workdir`.

## Cloud (free tier, no credit card)

- **Hosted backend + frontend: Render free web service**, built from the `Dockerfile` (see "Hosting"). This is the
  public, working prototype.
- **Optional: Cloud Firestore (Spark plan)** as a durable case store, so reviewer decisions survive container
  restarts. Setup steps are in the docstring of `sdv/cloud.py`; `python -m sdv sync push --db <batch>/cases.db`
  uploads and `sync pull` brings decisions back. Not exercised against a live project yet.

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

## Architecture and hosting

```
browser  --(static files)-->  frontend/  (any static host, or served by the backend)
browser  --(fetch, JSON)--->  backend API (sdv serve)  -->  workspace/<batch>/{data/, cases.db}
                                    |--> Jev (optional), vision model (optional)
```

The frontend knows the backend only through `frontend/config.js` (`window.SDV_API`) or a `?api=https://backend` address
parameter, so it can live on a different host (GitHub Pages, Netlify, Vercel: just publish the `frontend` folder). Then
start the backend with `--cors-origin https://your-frontend` (default `*`).

The repo ships a `Dockerfile`, `docker-compose.yml` and a `render.yaml` blueprint. The image processes the bundled
sample inbox (`demo/data`, synthetic) at build time and serves the API and the frontend on `$PORT`. Jev and the vision
model are OFF on a public copy (uploads use rules only), so nobody can spend your credits; set `SDV_JEV=auto` and a
`TYPESAFE_API_KEY` secret to change that.

```bash
docker compose up --build     # http://localhost:7860
```

Render (free web service, no card): New > Web Service > Public Git Repository > this repo > Runtime Docker > Free.
Set the health check path to `/api/health`. The copy running from this repo is https://shipping-doc-verifier.onrender.com.
A free service sleeps after 15 minutes without traffic (about a minute to wake); a free uptime pinger on
`/api/health` every 5 minutes keeps it awake. If you copy your local `.cache/jev/*.json` into `demo/jev_cache/` before
building, the hosted sample shows the classifications made with Jev and still needs no key. Free hosts have temporary
disk: uploaded batches and reviewer decisions last until the container restarts (Firestore sync is the durable option).
There is no login on the API; for a private deployment put it behind the host's access control.

## Scaling and limits

The unit of work is one email and emails are independent, so a run parallelises trivially: `process_all` already uses
a worker pool for model calls, and the pipeline itself is CPU-light (520 emails in about a second without models).
Model answers are cached by content, so a re-run costs nothing. At the volume the brief mentions (about 2,000 emails a
day) one small container is enough for the pipeline. What would change first: the API's built-in HTTP server and
SQLite case stores are fine for a team of reviewers, but a larger deployment would put a real web server and a hosted
database (Firestore or Postgres) behind the same `CaseStore` interface. Scans need a vision model or OCR; until one is
configured they are handed to a person, never guessed.

## Tests

```bash
python -m pytest -q                            # 80 tests
SDV_DATA=path/to/bundle python -m pytest -q   # also runs the end-to-end data test
```

## Validation (measured; reproduce with the commands shown)

| Check | Command | Result |
| --- | --- | --- |
| Rules-only pipeline vs the organizers' reference labels, 520 emails | `run --jev off` + `scripts/eval_local.py` | 0 disagreements |
| Jev vs the rule tier on all 520 emails (independent second opinion) | `audit` | 520/520 agree; Jev confidence median 1.0, min 0.60 |
| Stress test: 51 verified-clean emails, 3,213 controlled edits | `stress` | 100% in every class: benign 306 stay OK; defects 459 caught on exactly the edited field; blanks and removals 357 escalated; missing attachment, wrong document, corrupt PDF 153 escalated; unfamiliar labels 255 never a false mismatch; layout quirks 255 stay OK; defect plus layout quirk at the same time 1,326 caught; empty label followed by a measurement 102 escalated |
| Odd-PDF test: one document re-rendered as an unusual PDF (tables, value below label, rotated, watermark, multi-page, Chinese glosses, encrypted, abbreviated / renamed labels, 5 number formats) | `oddpdf --limit 30` | 2,520/2,520 (see "Odd-PDF test") |
| Unit and regression tests (incl. API upload / zip / link / hostile-path / attachment-escape tests) | `pytest` | 80 pass |

Read these with care:

- The 520-email corpus is templated. Agreement there shows the pipeline handles those templates, not unseen real mail.
- The stress edits were written by us, so they test the behaviours we thought of. They do not replace real
  variation such as new layouts, OCR noise on scans, or other languages.
- Scanned PDFs (no text layer) are escalated as `unreadable`; no OCR verdict is trusted.
- `stress --jev auto` measures how many unfamiliar labels Jev can resolve instead of escalating.
