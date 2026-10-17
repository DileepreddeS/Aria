# Fonts

**Carlito**, metric-compatible with Calibri, embedded in every resume so a PDF renders
the same on every machine (RESUME_ENGINE §1 rule 9: fonts embedded).

- Licence: SIL Open Font License 1.1 (`OFL.txt`, unmodified). Bundling and embedding in
  documents are permitted; the files are not sold on their own and keep their name.
- Source: github.com/google/fonts, `ofl/carlito/`, pinned at commit
  `3dd78844021e948ceb633d1dcee3f7885561b5d9` (2026-03-03), not the moving `main` branch.
- SHA-256, checked by `tests/test_fonts.py`, so a changed file is a failing test rather
  than a silent change in every resume:

| File | SHA-256 |
|---|---|
| Carlito-Regular.ttf | f6418f708baede9789daef5d458c0f53d2a888af9820e8062934e504fedc6595 |
| Carlito-Bold.ttf | bb5d20f79b82599ec72983597437373a80f2d2085fa91fc144fd74e876a594db |
| Carlito-Italic.ttf | 0b019225e58d702bfedcbd35c21696769f8ee115cb6343f84c2f240312450d1c |
| OFL.txt | 58402f82a7c332a700294988fe7554fbb0a63a8d27ccc1ee3bbc640311990a00 |

Ligatures are switched off in the stylesheet (`font-feature-settings: "liga" 0, "clig" 0,
"dlig" 0`): with them on, Carlito emits U+FB00-range characters, some with no Unicode
mapping, which extract as `(cid:NN)` (RESUME_ENGINE §10).
