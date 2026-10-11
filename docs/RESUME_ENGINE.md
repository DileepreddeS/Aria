# ARIA Resume Engine — Spec

A standalone engine. Input: one job description + one candidate. Output: a one-page, ATS-safe, recruiter-grade
PDF where **every claim is traceable to a verified fact**, plus an evidence map and a gap report.

It is a separate package (`engines/resume/`) with no knowledge of crawling, browsers or applying.
Other parts of ARIA call it only through its public API (section 10).

---

## 1. The ARIA Resume concept (the rules the engine exists to enforce)

1. **Truth first.** Every bullet, metric, skill, title and date comes from the candidate's verified facts.
   Nothing is invented, rounded, inflated or implied. If the evidence isn't there, the claim isn't made.
2. **Tailored story, not keyword makeup.** The same real experience is told differently per role type
   (frontend, backend, full-stack, AI, ML/research, data, DevOps): different lead experience, featured project,
   bullet selection, order and skills line. Never stuff JD keywords into bullets that don't support them.
3. **The 6-second recruiter test.** The top third of the page (summary, first 3 bullets, first skills line)
   must show the job's 3 most important requirements, with numbers.
4. **Bullet formula:** strong verb + specific technology + what was built/changed + exact measurable result.
   One or two lines. Allowed verbs come from a list per candidate voice (default: built, shipped, wired, added,
   trained, led, set up, sped up, improved, containerized, designed, cut, migrated, automated).
5. **No AI voice.** Banned words: leveraged, spearheaded, cutting-edge, synergy, robust, seamlessly, utilize,
   facilitate, innovative, streamlined, orchestrated, championed, transformative, impactful, holistic,
   paradigm, dynamic, proactive, revolutionized, best-in-class, world-class, game-changing, passionate,
   results-driven, detail-oriented, and similar. The list is configurable and versioned.
6. **Exact metrics.** "30%" stays "30%". "500K+" stays "500K+". Never round, never convert, never estimate.
7. **One page.** Always, for candidates with under ~8 years of experience.
8. **ATS-safe layout.** Single column, standard section headings, real text (no images of text), no tables
   for layout, contact info in the body (not the header/footer), embedded standard fonts.
9. **Design:** black body text, blue clickable links (email, LinkedIn, GitHub, portfolio), 10.5–11 pt body,
   12–14 pt name, 0.5–0.7" margins. Output is PDF. DOCX export is optional.
10. **Every resume is unique to its job,** and reproducible: the same inputs and versions produce the same output.

---

## 2. Pipeline

```
Resume / text (untrusted) ──► 0 Resume Intake ──► draft facts ──► user approval ──┐
                                                                                  │
JD text (untrusted) ──► 1 JD Parser ──► 2 Requirement Normalizer ──┐              │
                                                                   ▼              ▼
Candidate (verified facts) ──► 3 Candidate Graph ──────────► 4 Evidence Matcher
                                                                   │
                                                                   ▼
                                                5 Resume Composer (plan → write)
                                                                   │
                         ┌──────────────┬──────────────┬───────────┴──────────┐
                         ▼              ▼              ▼                      ▼
                   6a ATS check   6b Truth check   6c Recruiter check   6d Layout check
                         └──────────────┴──────┬───────┴──────────────────────┘
                                               ▼
                              fail → targeted repair (max 2 rounds) → fallback to verified fact text
                                               ▼
                                      7 Renderer → PDF + artifacts
```

Each stage is a pure function with typed (Pydantic) input and output, so it can be unit-tested and replayed.
LLM calls happen only in stages 1, 5b and 6c, and go through the LLM gateway (see SECURITY.md).
**Stage 0 uses no LLM at all** — it is geometry and rules, so its failures are inspectable and reproducible.

---

## 3. Stage 1 — JD Parser

The JD is **untrusted text**. It is passed as data, never as instructions (see SECURITY.md §5).

Output `ParsedJD`:
```python
class ParsedJD(BaseModel):
    title: str
    company: str
    seniority: Literal["intern", "new_grad", "junior", "mid", "senior", "staff_plus", "unknown"]
    min_years: int | None
    education: list[str]                 # "BS/MS in CS or related"
    location: str | None
    work_mode: Literal["remote", "hybrid", "onsite", "unknown"]
    responsibilities: list[str]          # verbatim-ish, max 12
    required: list[RawRequirement]       # text + section it came from
    preferred: list[RawRequirement]
    keywords: list[str]                  # exact surface forms as written in the JD ("PostgreSQL", "CI/CD")
    sponsorship_language: str | None     # quoted sentence if present
    clearance_or_citizenship: str | None # quoted sentence if present
    suspicious_instructions: list[str]   # anything telling the reader to do something unrelated to applying
```
Method: deterministic section splitting (Requirements / Qualifications / Nice to have / Responsibilities) and
regex for years, degrees and location, then one LLM call with a strict JSON schema for everything else.
`suspicious_instructions` feeds the security classifier. Their content is never acted on.

## 4. Stage 2 — Requirement Normalizer

Turns raw requirement text into canonical, weighted requirements.

```python
class Requirement(BaseModel):
    id: str
    kind: Literal["skill", "domain", "responsibility", "education", "years", "soft"]
    canonical: str              # "postgresql", "react", "distributed_systems"
    surface_forms: list[str]    # how the JD wrote it — used verbatim on the resume for ATS
    weight: float               # must-have 1.0, preferred 0.5, responsibility-implied 0.7
    must_have: bool
    min_years: int | None
```

Components:
- **Skill taxonomy** (`taxonomy/skills.yaml`, versioned): canonical id, aliases (React/React.js/ReactJS),
  parent/implies edges (Next.js → React → JavaScript; PostgreSQL → SQL; Kubernetes → containers),
  category (frontend, backend, data, ml, infra, language). Unknown terms are logged for review, never dropped.
- **Dedupe and merge** repeated requirements; keep the strongest weight.
- **Implicit requirements:** "build REST APIs in Node" → node.js, rest_api, backend.

## 5. Stage 0 — Resume Intake (uploaded document → draft facts)

An uploaded resume is **untrusted third-party output** in an unknown layout: Word, Google Docs, LaTeX,
Canva, novoresume, Overleaf, a browser print, or a scan. Intake must work on all of them, and must fail
loudly on the ones it cannot read. It never improves its odds by asking the user to reformat.

> **Why this section exists.** On 2026-10-11 an import of a Chromium-rendered PDF produced 0 facts and
> **9 fabricated roles** — an employer called `"mobile"` and a job title that was the text of a bullet —
> then reported `agreement 100%`. Two generator-side defects (CSS `float`, which makes Chromium write
> floated date boxes into the text layer ahead of the body, and CSS list markers, which never reach the
> text layer at all) produced a scrambled, marker-less text stream. The parser invented structure to fit
> it. **The defect is the parser's**, not the document's: it read flat text in content-stream order,
> had no shape checks, and reported an agreement metric as confidence.

### 5.1 Extract — geometry, never flat text

- **Primary:** `pdfplumber` (MIT) — `page.chars` gives every character with `x0`, `x1`, `top`, `bottom`,
  `fontname`, `size` and `upright`. Characters are merged into spans and lines by §5.2.
- **Cross-check:** `pypdf` (BSD-3) for the independent read in §5.6.
- **Never** parse the output of `extract_text()`. Content-stream order is whatever the generator happened
  to emit and carries no meaning. `pypdf.extract_text()` is used only as a cross-check signal (§5.6).

**Licensing constraint (binding).** PyMuPDF is AGPL-3.0 or paid-commercial. For a hosted product,
AGPL §13 would oblige offering source to every user over the network, so **PyMuPDF must not be used**
anywhere in ARIA. `pdfplumber` (MIT), `pdfminer.six` (MIT), `pypdf` (BSD-3) and `pypdfium2`
(Apache-2.0/BSD-3) are all acceptable and expose the same character-level geometry. Any new PDF
dependency is license-checked before it is added.

**Weight detection.** `pdfplumber` reports `fontname`, not a bold flag, so bold is inferred from the
PostScript name (`...-Bold`, `...-Black`, `...,Bold`, `...Semibold`). Subset fonts carry a six-letter
prefix (`ABCDEF+Carlito-Bold`) — strip it before matching. When a document's names carry no weight
information, fall back to comparing stroke width or glyph-area density against the modal body font, and
lower the confidence of every heading decision that depended on weight alone.

Record per page: char count, modal font size, font names, and whether each font carries a `ToUnicode` map.

**Triage before parsing.** Any row here ends the import with a reason; none of them are guessed through:

| Signal | Meaning | Result |
|---|---|---|
| 0 characters and ≥1 full-page image | scanned / image-only resume | `needs_user_fix`: no text layer; offer OCR path or another intake route (§5.8) |
| < 200 chars per page | mostly image | same |
| > 25% of chars are `(cid:N)` or U+FFFD | font has no usable `ToUnicode` map | `needs_user_fix`: font encoding unreadable |
| extraction blocked by permissions / encryption | protected PDF | `needs_user_fix`: locked document |

### 5.2 Reconstruct reading order (the core fix)

1. **Lines.** Merge spans sharing a baseline (y within ±0.3 × font size) into `Line` objects; line bbox is
   the union, line font attributes come from the dominant span.
2. **Columns.** Project line bboxes onto the x-axis and build a coverage histogram. A **gutter** is a
   contiguous x-range of zero coverage, at least ~4% of page width, spanning ≥60% of the page height.
   0 gutters → single column; 1 → two columns; ≥2 → multi-column. Guard: a gap present only in the top
   ~20% of the page is a header layout, not a column.
3. **Order.** Assign each line to a column, sort within a column by y then x, then concatenate columns
   left-to-right. This is the reading order; nothing downstream may consult the content stream.
4. **Running heads.** Lines in the top or bottom 6% of the page whose text repeats across ≥2 pages
   (page numbers, a repeated name banner) leave the body stream and are kept separately.

Output: an ordered `Line[]` carrying bbox, font, size, bold, column index and page.

This step alone removes the entire float-reordering class of bug and is what makes two-column templates —
where a flat extractor interleaves the skills sidebar into the experience bullets line by line — parse
correctly. Two-column layouts are a large share of resumes in the wild and are the single highest-value
case here.

### 5.3 Classify lines from visual signals

`body_size` = modal line font size. Classification uses geometry first; word lists only ever *raise*
confidence and are never the sole test, so that "WORK HISTORY", "Professional Experience" and
non-English headings all resolve.

- **SECTION_HEADER** — size > `body_size` × 1.08 **or** bold; under ~40 chars; and at least one of:
  all-caps, followed by a horizontal rule, matches the heading lexicon.
- **BULLET** — starts with a marker glyph (`• ◦ ▪ ‣ · - – *` or a lone `o`), **or** sits in a
  hanging-indent run.
- **Hanging-indent rule — how a wrapped line is told from a new bullet.** Within a run, line-start x
  values cluster on two points: `x_marker` and `x_text` (`x_text > x_marker`). A line starting at
  `x_marker` opens a new bullet; a line starting at `x_text` continues the previous one. This is purely
  geometric and works when the document contains **no marker glyphs at all** — which is exactly the case
  CSS `list-style` produces. It is what prevents a wrapped tail like `"mobile"` from becoming an entry.
- **ENTRY_HEADER** — bold, and either contains a separator (`—` `–` `|` `,`) or carries a date in the
  right-hand x-band.
- **DATE** — matched only within the right-hand x-band (`x0 > 0.55 ×` content width) of an entry header,
  or as its trailing token. Dates are never pattern-matched out of the middle of a line.
- **CONTACT** — in the first few lines, matching email / phone / URL patterns.
- **Word boundaries are rebuilt from character gaps, always.** Never use an extractor's word grouping.
  Within a line, insert a space where the gap between consecutive characters exceeds ~0.25 × font size.
  The reason is that extractor word grouping varies by version, not that any one extractor is
  broken. Chromium emits a positioning operator per character for the whole document, and any
  generator may do the same; how an extractor groups those characters into words is an implementation
  detail that changes between releases. On `per_glyph_contact.pdf`, `pypdf` 3.x splits the contact
  line into single characters while the pinned `pypdf` 6.19.0 reads it intact. A parser that leans on
  that grouping inherits whichever behaviour the installed version has. Rebuilding words from
  character gaps makes Stage 0 independent of it, and `per_glyph_contact.pdf` checks that
  inline-element boundaries do not perturb the rebuild.

### 5.4 Assemble entries

A section header opens a section. Entry headers inside EXPERIENCE / PROJECTS / EDUCATION open candidate
`Experience` / `Project` / `Education` entries. Bullets attach to the nearest preceding entry header
**in the reconstructed stream** — never in content-stream order. Employer and title come from splitting
the entry header on its separator and resolving by position plus the following line (usually a location).
Dates come only from the DATE token.

### 5.5 Shape rules and refusal gates (hard)

Per-entry rules. Any violation drops that entry and flags the import; a dropped entry is never silently
replaced by a guess.

- `employer` — ≤60 chars, ≤8 words, no terminal period, contains no metric (`\d+%`, `\d+\+`, `\d+K`),
  does not begin with an allowed resume verb.
- `title` — ≤60 chars, contains no metric, no terminal period.
- `dates` — parse to a real range; `end ≥ start`; `start ≥ 1950`; a start more than 12 months in the
  future is a flag, not a silent acceptance.
- each experience entry has ≥1 bullet, each bullet ≥20 chars.

Document gates. Any one failing returns `status = needs_user_fix` and **writes nothing**:

- ≥2 consecutive entry headers with zero bullets between them → reading order is suspect;
- entries ≥3 and facts == 0;
- >25% of entries failed a shape rule;
- >3 overlapping date ranges → likely cross-column bleed.

A failed import returns the reason, the reconstructed line stream, and a rendered page image, so the user
can see what ARIA saw rather than a bare error.

### 5.6 Cross-extractor check, reported honestly

Run `pypdf.extract_text()` and compare normalised token multisets against the geometric reconstruction.
Disagreement lowers confidence and raises a flag.

**Agreement is not evidence of correctness and is never surfaced as a score.** Two extractors reading the
same wrong content stream agree perfectly — that is precisely what reported `agreement 100%` over nine
fabricated roles. The field is `cross_extractor: agree | disagree`, a diagnostic. It is never a
percentage, never a quality metric, and never shown to the user as confidence.

### 5.7 The user confirms, always

Even at maximum confidence, intake produces **drafts only** and never writes verified facts (§6). It
shows the parsed structure back — entries with dates, bullets grouped under their entry, and everything
dropped with the reason — for approve / edit / reject per item. On a clean document this is a few
seconds' confirmation. On a mangled one it is the difference between a usable product and a wrong fact
graph the user never sees. This is "ask, don't guess" applied to intake, and it is the backstop behind
every rule above.

### 5.8 PDF is not the only door

All of these converge on the same draft-fact review step:

- paste plain text;
- `.docx` via `python-docx` — real structure, no geometry guessing; **preferred when available**;
- manual entry, or interview-only with no document at all;
- a user-supplied LinkedIn data export.

### 5.9 Test corpus (gates Phase 1)

`tests/fixtures/resumes/` — ≥50 real-world PDFs spanning Word, Google Docs, LaTeX (moderncv, Awesome-CV,
deedy), Canva, novoresume, Enhancv, Overleaf and Chromium-rendered HTML, plus one fixture for each known
pathology:

- float-scrambled reading order;
- marker-less bullets (CSS `list-style`);
- per-glyph-positioned contact line;
- two-column with a skills sidebar;
- scanned / image-only;
- ligature- and `(cid:N)`-corrupted.

Each carries hand-labelled ground truth (entries, employers, titles, date ranges, bullet counts).
Metrics per run: entry precision and recall, bullet-attachment accuracy, and **false-entry rate, which
must be 0** — inventing an entry is worse than missing one. The scanned and cid-corrupted fixtures must
**fail cleanly with a reason**, not parse. The original 2026-10-11 PDF pair (broken and corrected) is
kept as the first two fixtures.

## 6. Stage 3 — Candidate Graph

The candidate's career as a graph of **verified facts**. Stored in Postgres tables (nodes + edges). A graph
database isn't needed at this size.

Nodes:
| Node | Key fields |
|---|---|
| `Experience` | employer, title, start, end, location, client (e.g. Carvana via Accenture) |
| `Project` | name, type (academic / personal / research / open source), links, dates |
| `Education` | school, degree, field, start, end, gpa (optional), coursework |
| `Fact` | the atomic unit: one true statement, verbatim as the candidate approved it |
| `Metric` | exact string ("30%", "500K+", "mAP@0.5 = 0.700"), what it measures, baseline if any |
| `Skill` | canonical skill id from the taxonomy |
| `Achievement` | award, publication, certification, with date and link |
| `Motivation` | the candidate's own reasons, interests and preferences (used by the Answer Engine) |

Edges: `Fact —AT→ Experience|Project`, `Fact —USES→ Skill`, `Fact —PRODUCED→ Metric`,
`Fact —DEMONSTRATES→ Theme` (scale, performance, accessibility, reliability, leadership, ml_research, ...).

Every Fact has: `source` (resume import, onboarding interview, user edit), `verified: bool`,
`verified_at`, `sensitivity`, `version`. **Only verified facts can appear on a resume.**

Derived values (computed, never typed in by the LLM):
- years of experience per skill = union of date ranges of Experiences with Facts using that skill;
- total professional years; recency of each skill (last used).

Building the graph (onboarding):
1. Import an existing resume through **Stage 0 (§5)**, or use any of the other intake paths in §5.8 →
   draft Facts and Metrics. A failed import returns `needs_user_fix` and writes nothing.
2. Show each draft Fact to the candidate: approve / edit / reject. Nothing is verified automatically.
3. Enrichment interview: ARIA asks targeted questions to add missing metrics and context
   ("You built 20+ components. Do you know how many teams used them?"). Answers become new draft Facts.
4. Motivations interview: what problems you like, what you want next, team/company preferences.

## 7. Stage 4 — Evidence Matcher

For every Requirement, rank the Facts that prove it.

Score for a (Requirement, Fact) pair:
```
score = 0.40 * skill_match        # exact canonical match 1.0, implied via taxonomy 0.6, sibling 0.3
      + 0.25 * semantic_sim       # local embedding model, cosine(fact.text, requirement text)
      + 0.15 * metric_strength    # fact has a quantified metric; bigger relative impact scores higher
      + 0.10 * recency            # decays with years since the experience ended
      + 0.10 * context_fit        # professional > research > academic > personal for industry roles
                                  # (reversed for research roles)
```
Embeddings run **locally** (e.g. a small sentence-transformers model), so candidate data isn't sent to a third
party for matching. Weights are config and are tuned with the eval set.

Output `EvidenceMap`:
```python
class EvidenceMap(BaseModel):
    per_requirement: dict[str, list[ScoredFact]]   # top 3 each, with score breakdown
    coverage: float              # weighted share of must-haves with evidence above threshold
    strong: list[str]            # requirement ids
    light_gaps: list[Gap]        # weak evidence + closest fact + prep suggestion
    missing: list[Gap]           # no evidence
    role_type: str               # frontend | backend | fullstack | ai | ml_research | data | devops | swe
    fit_decision: Literal["strong", "ok", "weak", "no_fit"]
```
The gap report and the fit decision come straight from this. The screener (outside the engine) uses
`fit_decision`. The engine doesn't decide whether to apply.

## 8. Stage 5 — Resume Composer

### Stage 5a — Planner (deterministic, no LLM)
- Choose a **role template** (`templates/roles/*.yaml`): section order, lead experience rule, featured project
  rule, skills line order, summary pattern.
- Choose facts per section by Evidence Matcher score, with coverage constraints: each must-have with
  evidence gets at least one bullet; spread across experiences; most recent role gets 3–5 bullets,
  older roles 2–3, projects 2 each.
- **Line budget:** estimate rendered lines per bullet and pick the set that maximizes total requirement
  weight covered within one page (small knapsack). Re-check after rendering.
- Output `ResumePlan`: ordered sections, chosen fact ids per bullet slot, target requirement per bullet,
  the JD surface form to use for each skill.

### Stage 5b — Writer (LLM, strict)
For each bullet slot, the model receives **only**: the chosen Fact texts + their metrics + allowed tech
terms (fact tech + taxonomy aliases) + the target requirement + style rules. It returns:
```python
class Bullet(BaseModel):
    text: str
    fact_ids: list[str]          # must be a subset of the provided facts
    metrics_used: list[str]      # must be exact strings from those facts
    tech_used: list[str]         # must be in the allowed list
    requirement_ids: list[str]
```
Summary: 2 lines built from the top evidence (identity + years + strongest proof, then 3–4 must-have skills
that have evidence). Skills section: candidate's verified skills only, reordered so JD must-haves come first,
using the JD's surface forms (e.g. "PostgreSQL" rather than "Postgres" if the JD says so).

## 9. Stage 6 — Validators

All validators are code first, LLM only where noted. Each returns pass/fail plus actionable findings.

**6a ATS check**
- Parseability: render the PDF, extract text with two different extractors, and confirm reading order,
  headings, contact info and bullets survive. No text in images, headers or footers.
- **Round-trip through our own Stage 0.** Parse the generated PDF with the intake parser and require that
  entries, employers, titles, date ranges and bullet counts match the composer's plan exactly. Eating our
  own output is the strongest ATS test available, and it keeps generator and parser honest about each other.
- **Reading-order assertion:** every entry header is immediately followed by its own bullets in the
  extracted stream (the 2026-10-11 float defect, §5).
- **Glyph integrity:** no U+FB00–FB06 ligature characters and no `(cid:` in extracted text.
- **Contact line:** extracts as contiguous tokens, not per-glyph (`d i l e e p …` → fail).
- Standard headings: Summary, Education, Experience, Projects, Skills (configurable).
- Keyword coverage: share of must-have **evidenced** requirements whose JD surface form appears in the text.
  Also the acronym + long form where useful ("CI/CD", "Continuous Integration").
- Report-only score (keyword + TF-IDF). It never gates on its own; coverage of evidenced must-haves does.

**6b Truth check (the most important)**
- Every `metrics_used` string exists exactly in the cited facts, and no other number appears in the bullet.
- Every technology mentioned is in the cited facts' skills or their taxonomy implications.
- No employers, titles, dates, team sizes or scopes beyond the cited facts.
- **NLI entailment:** a local DeBERTa-v3 NLI model checks that the cited facts *entail* the bullet
  (same technique as SI-RAG's self-verification). Contradiction or neutral beyond a threshold → fail.
- Summary claims (years, domains) are recomputed from the graph and compared.

**6c Recruiter check**
- Banned words and phrases → fail. Weak openings ("Responsible for", "Worked on", "Helped") → fail.
- Allowed verb at the start of every bullet; at least 60% of bullets quantified; no duplicate verbs in a row.
- Vague phrase list ("cloud-native solutions", "modern web systems", "digital solutions") → fail unless
  literally in the fact.
- **6-second test (LLM, JSON):** given the top 3 requirements, does the top third of the page show each one?
  Fewer than 2 → fail with which ones are missing.

**6d Layout check**
- Exactly one page; no orphan lines; consistent date format; links clickable and correct; fonts embedded.

**Repair loop:** failures produce targeted fixes (rewrite only the failing bullet with the finding as
feedback, swap a fact, drop the lowest-value bullet for length). Max 2 rounds. If a bullet still fails, use
the verified Fact text itself, lightly formatted. The engine never ships a failing resume. It returns
`status=needs_review` with findings instead.

## 10. Stage 7 — Renderer and output

- HTML + CSS templates (Jinja2) → PDF through headless Chromium's print-to-PDF. Cross-platform, no MS Word
  dependency, real selectable text, embedded fonts. Optional DOCX export via python-docx from the same plan.

**Text-layer constraints for our templates** (verified against Chromium, 2026-10-11). The PDF text layer
is the product; what it looks like on screen is secondary. Each of these is enforced by a template lint
and by the §9 6a assertions:

| Rule | Why |
|---|---|
| No `float` for date columns — use `display: inline-block` | Chromium writes floated boxes into the text layer *ahead of* the body text, so every entry header is separated from its bullets. Verified: `inline-block`, table-cell and plain-line all preserve order; `float` alone breaks it. |
| No CSS list markers — emit a literal `•` with `list-style: none`, plus a hanging indent | CSS-generated markers never reach the text layer, leaving no way to tell a new bullet from a wrapped line. The hanging indent gives geometric parsers (§5.3) the second signal. |
| No styled `<span>` or `<a>` in the contact line — keep it unstyled and add links afterwards as PDF annotations from character coordinates | Any styled inline element makes Chromium position each glyph individually; `pypdf` then extracts `d i l e e p k u m a r …`. |
| `font-feature-settings: "liga" 0, "clig" 0, "dlig" 0` | Carlito/Calibri otherwise emit U+FB00-range ligatures, some with no Unicode mapping, which extract as `(cid:NN)`. |
| Right-hand date band at `x0 > 0.55 ×` content width | Matches the band §5.3 reads dates from, so our own output parses by the same rule everyone else's does. |
- File name: `First_Last_Resume.pdf` (no company names in the file name).
- Artifacts saved with every resume (`ResumeArtifact`):
  `pdf`, `plan.json`, `bullets.json` (with fact ids), `evidence_map.json`, `validation_report.json`,
  `diff_vs_base.json`, `input_hash`, `versions` (engine, taxonomy, templates, prompts, model ids).

**Public API**
```python
def build_resume(jd_text: str, candidate_id: str, options: ResumeOptions) -> ResumeResult
def explain(resume_id: str) -> EvidenceMap          # "why is this bullet here?"
def gap_report(resume_id: str) -> GapReport
```
`ResumeResult.status` ∈ {`ok`, `needs_review`, `no_fit`}.

## 11. Evaluation (required before the engine is used for real applications)

`evals/resume/`: 30 real JDs (6 per role type) with the expected must-haves labelled by hand.
Metrics per run: truth violations (must be 0), must-have coverage, 6-second pass rate, one-page rate,
banned-word count (must be 0), human rating 1–5 on 10 sampled resumes. Any change to prompts, taxonomy,
weights or templates must not lower these numbers. CI runs the deterministic parts. The LLM parts run on demand.

## 12. Same graph, other outputs

The Answer Engine (open-ended application questions, cover letters) reuses stages 1–4 and the Truth check,
plus the `Motivation` nodes. One source of truth for everything ARIA says about the candidate.
