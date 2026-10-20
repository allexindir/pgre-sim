# PGRE Simulator

A local exam simulator for GRE Physics practice tests. Import an ETS practice-book PDF once, then take it in your browser under test-day conditions. Scoring is automatic.

- **Importer** (`tools/importer.py`): turns a PDF into `exams/<id>/exam.json` plus cropped question images. Questions stay as images and are never transcribed.
- **Runner** (`runner/`): a static HTML/JS app with no dependencies and no build step. It delivers and grades any imported exam.

The full design is in [SPEC.md](SPEC.md).

## Setup

Requires Python 3.11+ and Node (only to run the scoring tests).

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r tools/requirements.txt
source .venv/bin/activate
```

Scanned PDFs also need `ocrmypdf` with tesseract.

## Import a test

```sh
python tools/importer.py pdfs/<file>.pdf [--id ID] [--expected N] [--pages FIRST-LAST]
```

The importer prints one summary line, then any FLAGS. It exits with `0` if clean, `1` if there are flags, or `2` on a fatal error. Each flag points to an overlay image in `exams/<id>/review/`.

Fix flagged problems in `exams/<id>/overrides.json`, such as answer keys, crop boxes, page range or score scale, and then rerun the importer. Don't edit `exam.json` by hand. To find coordinates without opening images, use:

```sh
python tools/importer.py pdfs/<file>.pdf --debug-page P
```

In Claude Code, the `import-test` skill runs this loop for you:

```sh
claude -p "Import pdfs/gr0877.pdf using the import-test skill"
```

## Take a test

```sh
python -m http.server 8000
```

Open http://localhost:8000/runner/. The runner offers:

- Timed or untimed mode, with a countdown that keeps running if you close the tab
- An ETS-style toolbar: Mark, Review, Reference (Table of Information), Back/Next
- Keyboard shortcuts: A–E answer, ←/→ navigate, M mark, R review
- Results with scaled score, percentile, raw-score formula, category breakdown, per-question timing and a pacing chart
- History export and import as JSON (attempts are stored in `localStorage`)

To spot-check an import on one page, open `http://localhost:8000/runner/?proof=<id>`.

## Tests

```sh
pytest tools/
node --test runner/scoring.test.js
```

## Note on content

ETS PDFs and everything derived from them stay local and are gitignored: `pdfs/`, `exams/*/img/`, `exams/*/review/` and `exams/*/source-ocr.pdf`. Don't commit them.
