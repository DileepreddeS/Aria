# ARIA — Product Spec (v2, built from scratch)

> **ARIA is a job-search agent, not a job-spam bot.**
> It understands the candidate, finds the right jobs, builds an honest tailored resume for each, answers
> application questions truthfully, applies like a person would, proves every submission, and learns what
> gets interviews.

ARIA is a product for many job seekers. The first user is the founder (new-grad SWE on F-1 OPT), and every
design decision must still work for user #1,000. Working name: ARIA. Pick a final brand before public launch
(the name collides with WAI-ARIA, the accessibility standard).

North-star metric: **interviews per 100 applications.** Not applications sent.

---

## 1. Principles

1. **Truthful by construction.** Resumes and answers come only from the candidate's verified facts and stated
   preferences. Form answers are never false, even when a false answer would pass a filter.
2. **Ask, don't guess.** Any doubt about the candidate (visa, preferences, an unknown question) goes to the
   candidate. Answers are saved and reused.
3. **The user decides policy.** ARIA hard-codes nothing about which jobs to apply to. Sponsorship, citizenship,
   location, seniority and company rules are user preferences with sensible defaults.
4. **Apply like a person.** It runs on the user's own device and browser, at human pace, in daytime windows,
   with the user solving any CAPTCHA. No CAPTCHA solvers, fingerprint spoofing, proxy rotation or stealth
   tooling. Never auto-apply on platforms whose terms forbid it (LinkedIn, Indeed): discovery only.
5. **Proof, not claims.** "Submitted" requires evidence (confirmation page or email) plus a receipt.
6. **LLM proposes, code decides.** State transitions, permissions and irreversible actions are controlled by
   deterministic code and policies, not by model output.
7. **Secure and private by design.** See SECURITY.md. Data minimization everywhere.
8. **Measured.** Every agent step is traced, costed and evaluated against fixed test sets.

## 2. System overview

```
                         CLOUD (multi-tenant)                                   USER DEVICE
 ┌───────────────────────────────────────────────────────────────┐      ┌────────────────────────┐
 │  Crawler ──► Job Store ──► Screener ──► Resume Engine          │      │  ARIA Runner           │
 │                                 │            │                 │      │  (desktop app)         │
 │                           Answer Engine ◄────┘                 │      │  ┌──────────────────┐  │
 │                                 │                              │ task │  │ Browser Agent    │  │
 │  Orchestrator (workflow state machines, policy engine) ────────┼─────►│  │ Playwright + CDP │  │
 │        │            │                  ▲                       │◄─────┤  │ dedicated Chrome │  │
 │   Liaison      Tracker / Outcomes      │ results, receipts     │      │  │ profile          │  │
 │  (asks user)   (email, analytics)      │                       │      │  └──────────────────┘  │
 │        │                               │                       │      │  sessions & cookies    │
 │  LLM Gateway   Credential Broker    Audit Log    Web Dashboard  │      │  never leave device    │
 └───────────────────────────────────────────────────────────────┘      └────────────────────────┘
```

Why a local runner: browser sessions and job-site logins stay on the user's machine (nothing valuable to
steal centrally); applications really come from the user's own browser and IP; we avoid running a bot farm.
The cloud does crawling, matching, writing and tracking. The device does the clicking.

## 3. Components

### 3.1 Crawler (shared across all users)
- Sources: Greenhouse, Lever, Ashby, Workable and SmartRecruiters public job-board APIs; schema.org `JobPosting`
  data from career-page sitemaps; public new-grad lists (e.g. SimplifyJobs on GitHub) for discovering
  company boards and sponsorship flags. LinkedIn and Indeed are not crawled.
  Endpoints verified Oct 2026:
  `boards-api.greenhouse.io/v1/boards/{slug}/jobs` (+ `/jobs/{id}` for content),
  `api.lever.co/v0/postings/{slug}?mode=json`,
  `api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true`,
  `apply.workable.com/api/v1/widget/accounts/{slug}`,
  `api.smartrecruiters.com/v1/companies/{slug}/postings` (verify).
- Crawl once, match many: one shared job store; per-user matching happens afterwards.
- Adaptive polling per board (hourly for active boards, daily for quiet ones), `ETag`/`If-Modified-Since`,
  per-host concurrency limits, jitter, robots.txt respected, backoff on 429/5xx.
- Change detection: new / edited / closed postings. Closing a posting cancels queued applications for it.
- Ghost-job scoring: age, repost frequency, evergreen patterns.
- Board health: consecutive failures park a board; slugs re-discovered automatically.
- Seed: ~1,580 boards on the five ATS, extracted from the Simplify new-grad data.
- Lessons from v1 to keep: filter titles and locations before any LLM call; handle multi-location postings;
  ", CA" may mean Canada; "Member of Technical Staff" is not senior; "U.S. person" / ITAR text means export
  control; ~28% of new-grad postings are on Workday (handled later by the generic agent).

### 3.2 Screener (per user)
Combines crawler data, the Resume Engine's Evidence Matcher and the user's preferences:
`FitDecision = apply | skip | ask_user`, with reasons. Preferences include:
- target roles and role mix, seniority, locations/remote, salary floor, company size/type, blocklist/allowlist;
- **policy per posting type** (each is Skip / Apply anyway / Ask me):
  "no sponsorship" postings, "citizenship/clearance/export control required" postings, "5+ years" postings,
  staffing agencies, unpaid/contract roles;
- sponsor signals: JD language + public USCIS H-1B Employer Data Hub history (ranking only, never hiding a job);
- daily caps, max applications per company per day/week, apply windows.

### 3.3 Resume Engine
See RESUME_ENGINE.md. Standalone, truth-validated, one page, PDF.

### 3.4 Answer Engine
Answers application questions using the same Candidate Graph:
1. **Classify** the question: identity, contact, work authorization, sponsorship, logistics (start date,
   relocation, schedule), compensation, EEO/demographics, experience-count, yes/no skill, open-ended
   (why us / why this role / about a project / challenge you solved), consent, knockout.
2. **Deterministic answers** for structured types, from the user's profile and preferences.
   Experience counts are computed from the graph. Never padded.
3. **Open-ended answers:** inputs = relevant Facts (via Evidence Matcher) + the user's Motivations +
   company facts extracted from the JD and the company's own pages (untrusted, facts only) + the user's
   previously approved answers to similar questions (retrieved by similarity, used as style and substance
   examples, never copied verbatim to a new company). Constraints: every claim traceable to the user's
   facts/motivations or a cited company source; at least one specific company detail; no invented personal
   anecdotes; no clichés; respect the field's length limit. Validated by the Truth check.
4. **Option matching** for dropdowns/radios: exact or near-exact match first; otherwise the model must
   choose from the given options only, with confidence. Low confidence → ask the user.
5. **Unknown or low-confidence → Liaison asks the user.** The answer is stored in the user's answer library
   with the question pattern and reused automatically next time.
6. **Knockout questions** (e.g. "Do you hold an active clearance?") follow the user's posting-type policy,
   and the answer is always true.

### 3.5 Liaison (human-in-the-loop)
One channel for every question to the user: dashboard inbox + push (Telegram in Phase 1, web/mobile push
later) with tap-to-answer buttons. Question types: missing profile info, unknown form question, policy
decision (ask-me postings), review request (dream companies, low confidence), CAPTCHA/login needed, security
review (suspicious posting or email). Every question has a deadline. On timeout, the application waits; it
never proceeds on a guess.

### 3.6 Browser Agent (runs inside the local Runner)
- **Stack:** Playwright for control (auto-waiting, frames, uploads) + a raw CDP session per page for perception.
  A dedicated Chrome profile for ARIA that the user signs into once.
- **Perception:** CDP `Accessibility.getFullAXTree`, `DOMSnapshot.captureSnapshot` (layout, visibility,
  styles), `DOM.getDocument(pierce)` for shadow DOM/iframes, `Network.*` and `Runtime`/`Log` events,
  `Page.captureScreenshot` with numbered element overlays. All fused and compressed into a field list:
  `{id, label, role, type, required, value, options, bbox, error_text, section}` + one screenshot.
- **Reasoning:** the planner model gets the field list, the screenshot and the trusted task. It returns a
  typed action plan. Page content is passed as untrusted data (SECURITY.md §5).
- **Acting:** every action passes the policy engine first (allowed domain, allowed field, allowed file,
  capability granted). Then act and verify: value actually set, no new error, expected state change.
- **Monitoring:** watches for new fields, validation errors, modals, redirects, CAPTCHAs, login walls, closed
  postings, and replans (max 2 replans per step).
- **Recipes:** after one success on a layout, store a deterministic recipe (field → selector → answer key) per
  ATS/tenant. Next time it runs without the LLM and falls back to the agent only when the page changed.
- **Adapters:** fast deterministic adapters for Greenhouse, Lever, Ashby, Workable, SmartRecruiters. The
  generic agent handles Workday, iCIMS, Taleo, SuccessFactors and custom portals (account creation +
  email verification there go through the Credential Broker and the user's approval).
- **Human pacing:** realistic typing and reading time, few applications per hour, apply windows only.

### 3.7 Orchestrator and state machines
Application lifecycle (persisted, resumable after a crash):
```
DISCOVERED → SCREENING → (ASK_USER) → TAILORING → VALIDATING ⇄ REPAIRING → READY
→ (AWAITING_APPROVAL) → DISPATCHED_TO_RUNNER → APPLYING → VERIFYING → SUBMITTED
→ FOLLOW_UP → OUTCOME{rejected, assessment, interview, offer, ghosted}
exits: SKIPPED · BLOCKED{captcha, login, closed, policy} · FAILED · WAITING_FOR_USER · CANCELLED
```
Browser session (inside the Runner):
```
OPEN → CLASSIFY_PAGE{form, login, create_account, captcha, closed, confirmation, unknown}
→ PERCEIVE → PLAN → POLICY_CHECK → ACT → VERIFY_STEP → (NEXT_PAGE | SUBMIT)
→ CONFIRM → RECEIPT → DONE
interrupts: CAPTCHA/LOGIN → WAIT_FOR_USER · UNKNOWN_QUESTION → ASK_USER · ERROR → REPLAN(≤2) → FAIL
```
Rules: transitions are validated in code; every state has a timeout and retry budget; every transition
writes an audit event; LLMs act only inside a state. Implementation: LangGraph with a Postgres checkpointer
for agent graphs; a Postgres `SKIP LOCKED` work queue for jobs between services. No extra broker until needed.

### 3.8 Autonomy levels (per user)
- **Suggest:** ARIA shortlists jobs and builds resumes; the user applies.
- **Approve:** ARIA prepares everything; the user taps approve per application; ARIA submits.
- **Autopilot:** ARIA applies within the user's rules and asks only when in doubt.
Dream companies are always Approve. A global and per-user **kill switch** stops all runners immediately.

### 3.9 Tracker, outcomes and insights
- Submission verification: confirmation page/URL, or a confirmation email.
- Outcome tracking from email: confirmation, rejection, assessment, interview, offer. Email access via a
  forwarding rule to a per-user ARIA inbox by default (avoids restricted Gmail scopes); OAuth Gmail later.
  Email content is untrusted (SECURITY.md §6).
- Insights: response rate by role type, ATS, sponsor signal, fit score, resume variant, company size.
- Daily report and nightly interview prep from gap reports.

### 3.10 Web dashboard
Today view, review queue (resume diff vs base, evidence map, gap report, open questions), answer library,
preferences and policies, candidate graph editor (approve/edit facts), applications with receipts,
insights, runner status, kill switch.

## 4. Agent messages (typed contracts)

All inter-component messages are Pydantic models in `packages/core/schemas`, versioned, never free text:
`JobPosting`, `FitDecision`, `ParsedJD`, `EvidenceMap`, `ResumeResult`, `QuestionSet`, `AnswerSet`,
`UserQuestion`, `UserAnswer`, `ApplyTask` (signed, short-lived, minimal data), `ActionPlan`, `ActionResult`,
`Receipt`, `OutcomeEvent`, `AuditEvent`.

## 5. Technology choices

| Area | Choice |
|---|---|
| Language (backend, engines, runner) | Python 3.12+, `uv` for env + lockfile |
| API | FastAPI, Pydantic v2 |
| Database | PostgreSQL 16 (Docker locally; managed Postgres in production), SQLAlchemy 2 + Alembic, row-level security per tenant |
| Workflows | LangGraph (agent graphs, checkpoints, human interrupts) + Postgres work queue |
| LLM access | Own LLM gateway service (LiteLLM inside) with data-class policies, cost tracking, provider failover |
| Local ML | sentence-transformers embeddings, DeBERTa-v3 NLI for truth checks |
| Browser | Playwright (Python) + CDP sessions, dedicated Chrome profile |
| PDF | Jinja2 HTML/CSS → Chromium print-to-PDF; pypdf + pdfminer for extraction checks |
| Web app | Next.js + TypeScript + Tailwind |
| Auth (beta+) | Managed OIDC provider with passkeys + MFA |
| Notifications | Telegram (Phase 1), web push + email (beta) |
| Observability | OpenTelemetry traces, structured JSON logs (PII-redacted), cost per application |
| Quality | pytest, ruff, mypy, pre-commit, gitleaks, pip-audit/osv-scanner, Semgrep |

## 6. Repository layout

```
aria/
├── apps/
│   ├── api/               # FastAPI modular monolith: users, preferences, jobs, applications, liaison, dashboard API
│   ├── web/               # Next.js dashboard
│   └── runner/            # local desktop runner: browser agent, recipes, receipts
├── engines/
│   ├── resume/            # Resume Engine (standalone package)
│   └── answers/           # Answer Engine
├── services/
│   ├── crawler/           # shared crawler + job store writer
│   ├── llm_gateway/       # only component allowed to call LLM providers
│   └── credentials/       # credential broker (isolated)
├── packages/core/         # schemas, state machines, policy engine, taxonomy, audit
├── evals/                 # resume/, answers/, browser/ (recorded pages), crawler/
├── data/seed/             # public seed data (company boards, taxonomy)
├── infra/                 # docker-compose, migrations, CI
├── docs/                  # PRODUCT_SPEC, RESUME_ENGINE, SECURITY, DECISIONS, THREAT_MODEL
├── private/               # gitignored: founder's personal data for local testing
└── CLAUDE.md
```

## 7. Build phases (each ends with "Done when")

| # | Phase | Done when |
|---|---|---|
| 0 | **Foundations:** repo, tooling, CI, Docker Postgres, core schemas, tenant model + RLS, audit log, config/secrets layout, LLM gateway skeleton, policy engine skeleton, tracing | CI green; cross-tenant read test fails as expected; audit events written for a sample transition |
| 1 | **Candidate Graph + onboarding:** secure upload → resume import → fact approval UI/CLI → enrichment + motivations interview | Founder's graph complete; every fact verified; skill-years computed correctly |
| 2 | **Resume Engine** end to end | 30-JD eval: 0 truth violations, 0 banned words, 100% one page, ≥90% 6-second pass, founder rates ≥8/10 resumes as "would send" |
| 3 | **Crawler + Job Store + Screener + preferences/policies** | Full crawl of seed boards; screener decisions explained; ask-me policies reach the Liaison |
| 4 | **Answer Engine + Liaison + answer library** | 50 real questions: 0 false answers; open-ended answers pass the Truth check; unknown questions reach Telegram and are reused |
| 5 | **Runner + Browser Agent:** perception, policy-gated actions, session state machine, 5 ATS adapters, recipes, receipts, verification | Recorded-page evals pass; 3 dry runs per ATS with complete receipts; then 2 verified live submissions per ATS with the founder watching |
| 6 | **Orchestrator + autonomy levels + scheduler + kill switch + daily report + dashboard** | A full day on Approve mode with zero unverified "submitted" states; kill switch stops the runner within 10 s |
| 7 | **Generic portals** (Workday, iCIMS, ...) + account creation + email verification | 10 verified Workday submissions with zero stored plaintext secrets |
| 8 | **Outcome tracking + insights** | A week of emails classified with ≥90% spot-checked accuracy |
| 9 | **Beta hardening:** managed auth, rate limits, security checklist (SECURITY.md stage B), privacy policy, data export/delete | External review of the checklist; 5–10 beta users onboarded |
| 10 | **Public launch readiness:** pen test, legal review, SBOM/signing, incident drills | SECURITY.md stage C complete |

The founder uses ARIA for real from Phase 5 on, in Approve mode.
