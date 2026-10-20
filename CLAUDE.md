# PGRE Simulator

A local, static exam simulator for GRE Physics practice tests. The full spec is in SPEC.md; read only the section you need.

## Layout
- `tools/importer.py` turns a PDF into `exams/<id>/exam.json`, `img/` crops and `review/` overlays.
- `exams/<id>/overrides.json` is the only place manual fixes go.
- `runner/` is the static HTML/JS app: vanilla ES modules, no dependencies, no build step.

## Commands
- Import: `python tools/importer.py pdfs/<file>.pdf [--id ID] [--expected N]`
- Inspect a page as text: `python tools/importer.py pdfs/<file>.pdf --debug-page P`
- Serve: `python -m http.server 8000`, then open http://localhost:8000/runner/ (add `?proof=<id>` to spot-check an import)
- Test: `pytest tools/` and `node --test runner/scoring.test.js`

## Rules
- Never read a source PDF (including `exams/<id>/source-ocr.pdf`) or view crop images in bulk. Open only the `review/` image named on a FLAG line, or use `--debug-page`.
- Never hand-edit `exam.json`. Edit `overrides.json` and rerun the importer.
- Questions stay as cropped images. Never transcribe questions or equations to text or LaTeX.
- Grading happens in the browser from `exam.json`. Claude never grades.
- Keep importer output terse: one summary line, then FLAGS. Add no other logging.
- Page numbers are 1-based PDF page indices. Coordinates are PDF points.
- Don't commit ETS content (`pdfs/`, `exams/*/img/`, `exams/*/review/`, `exams/*/source-ocr.pdf`).
