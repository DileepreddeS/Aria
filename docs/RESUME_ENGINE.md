# ARIA Resume Engine — Spec

A standalone engine. Input: one job description + one candidate. Output: a one-page, ATS-safe, recruiter-grade
PDF where **every claim is traceable to a verified fact**, plus an evidence map and a gap report.

It is a separate package (`engines/resume/`) with no knowledge of crawling, browsers or applying.
Other parts of ARIA call it only through its public API (section 9).

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
JD text (untrusted) ──► 1 JD Parser ──► 2 Requirement Normalizer ──┐
                                                                   ▼
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

## 5. Stage 3 — Candidate Graph

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
1. Import an existing resume (PDF/DOCX, through the secure upload pipeline) → draft Facts and Metrics.
2. Show each draft Fact to the candidate: approve / edit / reject. Nothing is verified automatically.
3. Enrichment interview: ARIA asks targeted questions to add missing metrics and context
   ("You built 20+ components. Do you know how many teams used them?"). Answers become new draft Facts.
4. Motivations interview: what problems you like, what you want next, team/company preferences.

## 6. Stage 4 — Evidence Matcher

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

## 7. Stage 5 — Resume Composer

### 5a. Planner (deterministic, no LLM)
- Choose a **role template** (`templates/roles/*.yaml`): section order, lead experience rule, featured project
  rule, skills line order, summary pattern.
- Choose facts per section by Evidence Matcher score, with coverage constraints: each must-have with
  evidence gets at least one bullet; spread across experiences; most recent role gets 3–5 bullets,
  older roles 2–3, projects 2 each.
- **Line budget:** estimate rendered lines per bullet and pick the set that maximizes total requirement
  weight covered within one page (small knapsack). Re-check after rendering.
- Output `ResumePlan`: ordered sections, chosen fact ids per bullet slot, target requirement per bullet,
  the JD surface form to use for each skill.

### 5b. Writer (LLM, strict)
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

## 8. Stage 6 — Validators

All validators are code first, LLM only where noted. Each returns pass/fail plus actionable findings.

**6a ATS check**
- Parseability: render the PDF, extract text with two different extractors, and confirm reading order,
  headings, contact info and bullets survive. No text in images, headers or footers.
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

## 9. Stage 7 — Renderer and output

- HTML + CSS templates (Jinja2) → PDF through headless Chromium's print-to-PDF. Cross-platform, no MS Word
  dependency, real selectable text, embedded fonts. Optional DOCX export via python-docx from the same plan.
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

## 10. Evaluation (required before the engine is used for real applications)

`evals/resume/`: 30 real JDs (6 per role type) with the expected must-haves labelled by hand.
Metrics per run: truth violations (must be 0), must-have coverage, 6-second pass rate, one-page rate,
banned-word count (must be 0), human rating 1–5 on 10 sampled resumes. Any change to prompts, taxonomy,
weights or templates must not lower these numbers. CI runs the deterministic parts. The LLM parts run on demand.

## 11. Same graph, other outputs

The Answer Engine (open-ended application questions, cover letters) reuses stages 1–4 and the Truth check,
plus the `Motivation` nodes. One source of truth for everything ARIA says about the candidate.
