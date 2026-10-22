# Phase 2 build plan: the Resume Engine

The approved order of work for PRODUCT_SPEC §7 Phase 2, with what each step must show before
it counts as done. The engine itself is specified in `RESUME_ENGINE.md`; the reasons behind
each choice are in `DECISIONS.md`. Each step is one or more commits, replayed with mutation
checks before hand-over.

**Phase done when** (PRODUCT_SPEC §7): on the 30-posting eval, 0 truth violations, 0 banned
words, 100% one page, at least 90% pass the 6-second test, and Dileep rates at least 8 of 10
sampled resumes "would send".

| # | Step | Status |
|---|---|---|
| 1 | Package skeleton, schemas, `CandidateSnapshot`, the `LlmClient` protocol and a fake | done |
| 2 | Stage 7 renderer, template lint, link annotations, 6d layout check, 6a ATS round-trip | done |
| 3 | 6b truth and 6c recruiter checks (deterministic parts), `style.yaml` | done |
| 4 | Stage 1 rules and Stage 2 normalizer | done |
| 5 | Stage 4 scoring and the local embedding model | done |
| 6 | Stage 5a planner, line budget, fit-to-page | done |
| 7 | Eval harness, three synthetic postings, deterministic metrics in CI | done |
| 8 | Real provider in the gateway, and the Stage 1 LLM step | 8a, 8b done; 8c in review; 8d next |
| 9 | Stage 5b writer and the repair loop | |
| 10 | NLI truth check and the 6-second check | |
| 11 | Public API, migration 0009, API routes, CLI, artifacts | |
| 12 | The 30-posting eval: tune, sample 10 for rating, report against "done when" | |

## Done when, per step from 7 on

Each list is the step's own: a step is not done while any item is open, and an item is
removed only by a recorded decision, never by being forgotten.

### 7. Eval harness

- `uv run python -m evals.run resume-synthetic` runs every synthetic posting through the
  deterministic pipeline; CI runs it (`make eval`) after Chromium is installed.
- Gated: truth violations 0, banned words 0, one page after fitting, layout findings 0, ATS
  findings 0, and exact agreement with each synthetic posting's labels.
- Reported only: coverage, fit, role type, the other recruiter findings, fitting rounds.
- Each run writes an untracked report; the committed baseline changes only with
  `--update-baseline`, never from a `--models` run, never while a gate fails.
- The report states that the gates rest on three synthetic postings.

### 8. Real provider and the Stage 1 LLM step

- Provider due diligence recorded in DECISIONS before the first call (retention, training,
  data retention, region).
- 8b: the gateway refuses an unpriced model and caps spend per run and per month from persisted usage,
  before sending; a test fails if the pinned SDK starts requiring OpenTelemetry.
- 8c: the LLM step reads untrusted posting text only through the quarantined pattern (SECURITY §5),
  keeps only verbatim quotes, never overrides the rules, and falls back to them on any failure.
- 8d: its output passes the same label gate as the rules (`evals.run --llm`, reported, never in CI),
  and a red-team set of injected postings runs in CI against scripted hostile replies.

### 9. Writer and repair loop

- One bullet per slot, each checked against the facts it was given; the repair loop rewrites,
  swaps a fact or drops the lowest-value bullet, at most two rounds, then falls back to the
  facts' own words or returns `needs_review`.
- **Flip recruiter findings to gated.** In `evals/resume/suite.py` every 6c finding (opening
  verbs, quantified share, vague phrases), not only banned words, fails the eval, and the
  baseline is updated in the same commit. Until then they are reported only, because the
  fallback page prints the facts' own opening verbs (DECISIONS 2026-10-19).

### 10. NLI and the 6-second check

- DeBERTa-v3-base NLI (xsmall as the fallback) added to `models.lock`; the 6-second check runs
  through the gateway.

### 11. API, storage, CLI

- Migration 0008, `POST /resumes` and the read routes, `aria resume`, `aria explain`,
  `aria gaps`; the PDF stored under a generated name.

### 12. The 30-posting eval

- The real postings in `evals/resume/jds/` (see its README), 6 for each of the five role types,
  measured against the phase's "done when", and 10 resumes sampled for Dileep's rating.
