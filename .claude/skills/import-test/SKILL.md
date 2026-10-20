---
name: import-test
description: Import a GRE Physics practice-test PDF into the exam runner and fix anything the importer flags. Use when asked to import or add a practice test PDF.
---

1. Run `python tools/importer.py <pdf>`. Add `--id <id>` or `--expected N` if the user gave them.
   Run it as one plain command: no `&&`, `||`, `;`, pipes or `echo`, which the permission rules don't allow.
   If `python` isn't found, use `.venv/bin/python tools/importer.py <pdf>` instead, here and below.
2. Exit 0: reply with the summary line. Done.
3. Exit 2: reply with the error line. Stop.
4. Exit 1: handle each FLAG.
   - Open only the review image named on that flag's line. For exact text coordinates, use `--debug-page <p>` instead of viewing more images.
   - Fix it in `exams/<id>/overrides.json` (create the file if missing): crop boxes in PDF points, answers, set ranges, scoring rule, time limit or scale.
   - If the overlay shows the crop is actually correct (e.g. graphical answer choices), add the flag to `ignoreFlags` instead, as `"CODE:target"` (e.g. `"SPLIT:q5"`).
   - Other keys are listed in SPEC.md §4.2.
   - Never edit exam.json, never open the source PDF, never view img/ crops in bulk.
5. Rerun the importer. Stop after at most 3 fix-and-rerun cycles.
6. Reply in 5 lines or fewer: the summary line, what you overrode, any flags left, and
   "Spot-check: http://localhost:8000/runner/?proof=<id>".
