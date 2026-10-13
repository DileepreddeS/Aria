# Resume intake fixtures

Corpus for RESUME_ENGINE §5.9. Target: ≥50 real-world PDFs across Word, Google Docs, LaTeX
(moderncv, Awesome-CV, deedy), Canva, novoresume, Enhancv, Overleaf and Chromium-rendered HTML,
plus one fixture per known pathology.

**Identity in every fixture is synthetic** (Jordan A. Rivera, `example.com`, `(555) 01x-xxxx`).
Git history is permanent and this corpus should stay shareable. Layout pathology is what these test;
the name on the page is irrelevant. Never commit a real person's resume, including your own.

Each fixture has a `<name>.truth.json` alongside it:

```json
{
  "fixture": "clean_inline_block.pdf",
  "pathology": "none — reference rendering",
  "must_fail": false,
  "fail_reason": null,
  "entries": [
    {"section": "experience", "bullet_count": 7, "employer": "Nordvale Consulting",
     "title": "Software Engineer", "start": "2024-02", "end": "2025-07"}
  ]
}
```

All five share the same 9 entries, so a difference in a fixture's result is attributable to its
pathology alone.

Metrics: entry precision/recall, bullet-attachment accuracy, and **false-entry rate, which must be 0**.
Inventing an entry is worse than missing one.

## Current fixtures

All five are the same document rendered five ways, so a parser difference is attributable to the
pathology alone.

| File | Pathology | Expected |
|---|---|---|
| `clean_inline_block.pdf` | none — the reference rendering | parses fully; round-trips |
| `float_scrambled.pdf` | CSS `float` date columns: Chromium writes all 8 entry headers into the text layer ahead of the body, detaching every role from its bullets | geometric reconstruction (§5.2) recovers correct order; a flat-text parser must trip a §5.5 gate (on the pinned `pypdf` it is "more than a quarter of entries fail a shape rule": 6 of 20). Also the §5.6 *disagreement* fixture: on the pinned `pypdf` (6.19.0) `extract_text()` emits the headers first while `pdfminer.six` reads visual order — the run must flag the mismatch, still produce baseline entries, and never prefer the cross-check's reading |
| `markerless_bullets.pdf` | CSS `list-style: disc` — no bullet glyph reaches the text layer, but Chromium draws each disc as a vector shape (27) | **drawn markers**: Stage 0 reads each shape as the bullet's marker; a glyph-only rule emits wrapped-line fragments as entries, and its flat text trips the §5.5 gate "two or more roles in a row with no bullets" |
| `no_marker_bullets.pdf` | bullets with no marker at all, glyph or drawn: bullets start at x=40, wraps at x=49, the entry edge is x=36 | **the hanging-indent rule (§5.3)**: wraps prove the column's x_marker, and every plain line starting there inside an entry is a bullet, wrapped or not |
| `per_glyph_contact.pdf` | styled `<span>`/`<a>` in the contact line — Chromium writes `Td`/`Tm` operator boundaries at every inline element | **baseline equivalence**: the same nine entries and the same contact string as `clean_inline_block.pdf`. Tests that inline-boundary operators do not perturb character-gap word grouping (§5.3) |

These reproduce the 2026-10-11 regression, where the first parser emitted 0 facts and 9 fabricated roles
(employer `"mobile"`, a bullet as a job title) and reported `agreement 100%`. Each must now parse
correctly via geometry, or fail cleanly with a reason — never emit an entry absent from the truth file.

## Still needed

Scanned/image-only · ligature + `(cid:N)` corrupted · two-column with skills sidebar · LaTeX moderncv ·
Canva export · Word tabular dates · a non-English heading set.
Use public template PDFs and replace any identity in them with the synthetic one above.
