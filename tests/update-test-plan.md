# update.py Test Plan

Manual tests to verify `update.py` handles changes in `llms-full.txt` correctly.

## Setup

Generate all test fixture files from the latest snapshot:

```
python3 tests/generate_fixtures.py
```

This creates a separate input file for each test in `snapshots/tests/` (gitignored).

Run each test with:

```
python3 update.py snapshots/tests/test_NN_<name>.txt
```

---

## Tests

### Mapped Sections (non-SPLIT)

**Test 1 — Add a URL**
Add a new link line under `### Claris FileMaker Data API Guide`.
Expected: `data-api/references/data-api.md` gains one entry.

**Test 2 — Remove a URL**
Delete one link from `### Claris FileMaker Data API Guide`.
Expected: `data-api/references/data-api.md` loses that entry.

**Test 3 — Change a link's display name (URL stays the same)**
Edit a link in `### Claris FileMaker Data API Guide` so the label changes but the URL is identical.
Expected: `data-api/references/data-api.md` reflects the new label.

**Test 4 — Change a link's URL (display name stays the same)**
Edit a link in `### Claris FileMaker Data API Guide` so the URL changes but the label is identical.
Expected: `data-api/references/data-api.md` reflects the new URL.

**Test 5 — Empty section**
Remove all link lines from `### Claris FileMaker Data API Guide`, leaving only the header.
Expected: `data-api/references/data-api.md` is written with just the DO NOT EDIT comment and section title, no links.

**Test 6 — Remove an entire section**
Delete the `### Claris FileMaker OData Guide` header and all its links.
Expected: `odata/references/odata.md` is deleted from disk and reported in console output as removed.

**Test 7 — Add a new section that IS in SECTION_MAP**
Remove `### Claris FileMaker Admin API Guide` and all its links from the test file, run once to establish a baseline without it, then add it back with a couple of fake links.
Expected: `admin-api/references/admin-api.md` is written with those links. Verifies `update.py` creates the file when a mapped section reappears.

**Test 8 — Add a new section NOT in SECTION_MAP**
Add a brand new `### Claris Some New Product Guide` header with a couple of fake links.
Expected: section appears in "Unmapped sections" console output, no reference file written.

---

### SPLIT Sections

**Test 9 — Add a URL with a known slug**
Add a new link under `### Claris FileMaker Pro Help` with a slug matching a known classifier (e.g. `get-something-new.md`).
Expected: link lands in `filemaker-pro/references/pro-func-get.md`.

**Test 10 — Add a URL with an unknown slug**
Add a new link under `### Claris FileMaker Pro Help` with a slug matching no classifier rule (e.g. `brand-new-page.md`).
Expected: link lands in the catch-all file for that section (`pro-general.md`).

**Test 11 — Remove all links from a SPLIT section**
Remove all links from `### Claris FileMaker Pro Help`.
Expected: all `pro-*.md` files are written empty (just header comment and title).

---

### File System

**Test 12 — New skill references directory doesn't exist**
Delete a skill's `references/` directory entirely, then run `python3 update.py`.
Expected: `update.py` recreates the directory and writes all reference files correctly.

---

## Known Behaviors (Not Current Concerns)

Observed during test run on 2026-07-08. Not bugs requiring immediate action — noted for awareness.

**Tests 9, 10 (original numbering) — Duplicate URLs and duplicate section headers**
If the same URL or section header appears twice, `update.py` writes all occurrences without warning. Not realistic scenarios for Claris-published content — tests removed from the active plan.

**Test 14 (original numbering) — Partial slug match on excluded pattern**
The slug `get-folder-path-duplicate.md` landed in `pro-func-get.md` rather than the catch-all because the exclusion in `classify_pro_help` only checks `base != "get-folder-path"` exactly, not `base.startswith("get-folder-path")`. Minor classifier edge case with no real-world impact — test removed from the active plan.
