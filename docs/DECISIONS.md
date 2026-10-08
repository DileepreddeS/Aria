# Decisions log

Format: date — decision — why — (revisit when)

- 2026-10-07 — Rebuild ARIA from scratch in a new repo; v1 kept as reference only — v1 architecture (single loop, keyword-injection rewriter, job boards) doesn't fit the product. Starting clean gives full understanding and control.
- 2026-10-07 — ARIA is built as a product for many job seekers; founder is user #1 — every design choice must work multi-tenant.
- 2026-10-07 — Hybrid architecture: cloud "brain" + local Runner doing browser work on the user's device — sessions and logins never leave the device; applications genuinely come from the user's browser and IP; removes central credential and cookie risk.
- 2026-10-07 — Browser stack: Playwright for control + raw CDP for perception (AX tree, DOMSnapshot, network, screenshots). No Puppeteer, Selenium or custom Chromium — CDP already provides all page information; more libraries add bugs, not information.
- 2026-10-07 — Workflows: explicit state machines; LangGraph with Postgres checkpoints for agent graphs; Postgres SKIP LOCKED queue between services. LLMs act inside states; code controls transitions.
- 2026-10-07 — Modular monolith + three isolated components (Runner, Credential Broker, LLM Gateway) instead of microservices — security isolation where it matters without operational sprawl.
- 2026-10-07 — Resume Engine is a standalone package: JD Parser → Requirement Normalizer → Candidate Graph → Evidence Matcher → Composer (plan, write) → ATS / Truth / Recruiter / Layout validators → PDF.
- 2026-10-07 — Job-type policies (no sponsorship, citizenship/clearance, years, staffing) are user preferences (Skip / Apply anyway / Ask me), not hard-coded skips. Form answers are always truthful regardless.
- 2026-10-07 — No CAPTCHA solving or anti-bot evasion. Users solve CAPTCHAs via the Liaison. Human pacing and the user's own browser instead.
- 2026-10-07 — LinkedIn and Indeed: discovery only, no automated applying (terms of service, account-ban risk).
- 2026-10-07 — Email intake defaults to a forwarding rule to a per-user ARIA inbox — avoids restricted Gmail scopes and their annual security assessment. (Revisit at beta.)
- 2026-10-07 — Local embeddings and NLI models for matching and truth checks — keeps candidate profiles away from third-party providers; reuses SI-RAG NLI verification.
- 2026-10-07 — PDF rendering via HTML/CSS → Chromium print-to-PDF, not docx2pdf — cross-platform, no MS Word dependency, ATS-friendly text layer.
- 2026-10-07 — Product name "ARIA" is a working name; collides with WAI-ARIA. (Revisit before public launch.)
