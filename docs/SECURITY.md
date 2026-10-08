# ARIA — Security & Privacy Architecture

ARIA holds some of the most sensitive data a person has during a job search: identity, contact details,
work history, visa/immigration status, demographic (EEO) answers, and the ability to act on their behalf on
job sites. Security is a design input, not a later phase.

Standards we follow:
- **OWASP ASVS 5** is the engineering checklist (target Level 2, Level 3 for credential and agent components).
- **OWASP Top 10:2025** for general application threats, including Software Supply Chain Failures.
- **OWASP API Security Top 10** for the API.
- **OWASP guidance for LLM and agentic applications** (prompt injection, excessive agency, sensitive data
  disclosure, insecure output handling, tool misuse).
- **NIST SSDF (SP 800-218)** for the development lifecycle.
- **NIST Privacy Framework** for personal-data handling.
- **NIST SP 800-63-4** for authentication and identity.

When this document and convenience conflict, this document wins. Exceptions are recorded in `docs/DECISIONS.md`
with a reason and an expiry date.

---

## 1. Assets and threat model (summary — full STRIDE in THREAT_MODEL.md)

| Asset | Why it matters | Main threats |
|---|---|---|
| Candidate PII (name, email, phone, address) | identity theft, spam | DB breach, logs, LLM provider exposure |
| Sensitive classes (visa/immigration status, EEO answers, disability, veteran) | discrimination, legal harm | over-collection, cross-tenant leaks, sending to LLMs |
| Job-site sessions and accounts (Workday etc.) | an attacker can act as the user | cookie theft, credential storage breach |
| OAuth tokens (email, calendar) | full mailbox access | token theft, over-broad scopes |
| Resumes and the candidate graph | the product's core data | tampering, exfiltration via the agent |
| The agent's ability to act | submitting, uploading, emailing on the user's behalf | prompt injection, malicious postings/emails, confused deputy |
| Platform credentials (DB, LLM keys, signing keys) | everything | leaked `.env`, supply-chain compromise |

Trust boundaries: user device ↔ cloud API; API ↔ database; API ↔ LLM gateway ↔ providers; crawler ↔ the
internet; Runner ↔ job sites; email inbound ↔ ARIA; third-party packages ↔ our code.

## 2. Architecture rules

1. **Not one giant app.** A modular monolith for the core API, plus three isolated components with their own
   credentials and network rules: the **Runner** (hostile web content), the **Credential Broker** (secrets),
   and the **LLM Gateway** (third-party AI). The crawler runs as a separate worker with no access to user data.
2. **The browser agent is hostile.** It runs on the user's device inside the Runner, with no database
   credentials, no platform API keys, and no ability to read other users' data. It receives one signed,
   short-lived `ApplyTask` containing only what that application needs, and returns results.
3. **Separate data from instructions.** Untrusted content is always passed as data in a dedicated field, never
   concatenated into instructions (§5).
4. **Least privilege everywhere:** per-service DB roles, per-capability agent tools, narrow OAuth scopes,
   per-environment secrets.
5. **Internal services are not exposed.** Only the API gateway and web app are public. The database, queue,
   LLM gateway and credential broker are reachable only on the private network.

## 3. Data classification and handling

| Class | Examples | Storage | Who/what may read | Sent to LLM? |
|---|---|---|---|---|
| **S3 — Secret** | passwords, OAuth refresh tokens, session cookies, API keys | Credential Broker only, envelope-encrypted (KMS), never in the main DB | Credential Broker; injected at use time | **Never** |
| **S2 — Sensitive PII** | visa/immigration status, EEO answers, disability, veteran, DOB, home address, government IDs (not collected) | separate schema, field-level AES-256-GCM with per-tenant data keys | API with explicit purpose; deterministic form filling | **Never** (answers are deterministic) |
| **S1 — Personal** | name, email, phone, work history, facts, resumes, motivations | main DB, encrypted at rest, RLS per tenant | API, engines (minimized) | only the minimum fields for the task |
| **S0 — Public / non-personal** | job postings, company data, taxonomy | main DB | everyone internal | yes |

Rules:
- Collect only what the product needs. Government ID numbers, SSN and DOB are **not collected**. If a form
  asks for them, the application goes to the user.
- Every column carries its class in code (`Annotated[str, Sensitivity.S2]`). Serializers, loggers and the
  LLM gateway enforce class rules automatically.
- Logs never contain S2/S3 data. S1 data in logs is redacted to IDs.

## 4. Policy engine and agent capabilities

All agent actions go through `packages/core/policy` before execution. **The LLM proposes, the policy engine
decides.**

Capabilities (granted per task, never "everything"):
`READ_JOB`, `READ_APPLICATION`, `FILL_FIELD`, `UPLOAD_RESUME`, `SUBMIT_APPLICATION`, `NAVIGATE`,
`READ_EMAIL_SUMMARY`, `SEND_EMAIL` (not granted in v1), `USE_CREDENTIAL`, `CREATE_ACCOUNT`.

Example policy checks:
- `NAVIGATE(url)`: host must be the posting's ATS host or an allowlisted SSO/asset host for that ATS; no
  private IPs; no downloads.
- `FILL_FIELD(field, value_ref)`: the value comes from an answer key approved for this task, not from free
  text produced while reading the page; S2 values only into fields classified as the matching question type.
- `UPLOAD_RESUME(file_id)`: only the PDF generated for this application (hash-checked).
- `SUBMIT_APPLICATION`: only if autonomy level allows, all required fields resolved, no open user questions,
  posting still open, and the daily/company caps are not exceeded.
- `USE_CREDENTIAL(site)`: the broker injects the secret directly into the field; the agent and LLM see only
  "credential filled".
- Anything outside the expected flow (a page asking to email documents, a new domain, a request for ID
  scans, payment, or a "message the recruiter" step) → stop and ask the user.

Tool design: narrow getters only — `get_candidate_name()`, `get_candidate_email()`,
`get_facts_for_requirements(ids)`, `get_resume(file_id)`. No `get_user_data()`.

## 5. Prompt-injection defense (job postings, web pages, emails, PDFs)

Untrusted sources: website text and DOM, job descriptions, application questions, emails, recruiter messages,
PDFs and DOCX, company pages. Trusted: system policy, developer policy, the user's explicit instructions
and approved profile data.

Layers (all required; structured prompts alone are not enough):
1. **Structural separation:** untrusted content travels in `untrusted_content` fields with a source label;
   the system prompt states that it is data and can never change the task. Users cannot edit system prompts.
2. **Two-model pattern:** a *quarantined* model reads untrusted content and may only output schema-validated
   data (extracted requirements, field lists, summaries). It has no tools. The *privileged* planner never
   sees raw untrusted text, only the quarantined model's structured output.
3. **Policy engine** (§4) checks every action no matter what the model says.
4. **Suspicious-instruction classifier** on postings, pages and emails: requests for documents (passport,
   SSN, bank details), off-site uploads, payment, urgent "click here", instructions addressed to AI agents.
   Hit → block or user review, and the posting is flagged for all users.
5. **Output handling:** model output is never executed, never rendered as raw HTML, and never used as a URL
   without validation.
6. **Evals:** a red-team set of injected postings, pages and emails in `evals/security/` runs in CI.

## 6. Email is hostile

Flow: `email → untrusted store → quarantined model summarizes/classifies → policy evaluation →
deterministic action or user decision`. Never `email → LLM → action`.
- Links are never auto-opened; attachments are never auto-opened; HTML is rendered sanitized with remote
  images blocked (no tracking pixels).
- Only classification (confirmation / rejection / assessment / interview / offer / other) and extracted
  dates/links are produced; any action (scheduling, replying, uploading) needs the user.
- Default intake: user forwarding rule to a per-user ARIA address (avoids restricted Gmail scopes and their
  annual security assessment). OAuth email access later, with the narrowest scope that works.

## 7. File uploads

`upload → quarantine bucket → size limit (10 MB) → magic-byte type check (PDF, DOCX, TXT, PNG/JPG only) →
malware scan (ClamAV) → parse in a sandboxed worker with timeouts → produce a sanitized derivative (extracted
text + our own regenerated PDF) → application storage`. The original is deleted after processing unless the
user opts to keep it. Never trust filename, extension or client-supplied MIME type. Macros and embedded
objects are never executed. Images used for OCR are re-encoded.

## 8. Browser Runner (user device)

- Dedicated Chrome profile for ARIA, separate from the user's personal browsing.
- Runner authenticates to the API with a device-bound key pair (registered once with the user's approval).
  Tasks are signed by the API and expire in minutes.
- Session cookies and job-site logins never leave the device and are never sent to the cloud or an LLM.
- In-browser request routing blocks navigation outside the task's allowlist, blocks downloads, and blocks
  file chooser access except the task's resume file.
- Screenshots and receipts are uploaded with S1 redaction where possible; S2 fields are masked in screenshots.
- Auto-update only through signed releases; the Runner verifies signatures before installing.

## 9. Network and SSRF

- Crawler and any server-side fetcher run behind an egress proxy: HTTPS only, allowlisted ports, private,
  loopback, link-local and metadata IP ranges blocked (`10/8`, `172.16/12`, `192.168/16`, `127/8`,
  `169.254/16`, `::1`, `fc00::/7`, cloud metadata hosts).
- **Don't trust DNS:** resolve once, validate the IP, connect to that exact IP (prevents DNS rebinding);
  re-validate on every redirect; cap redirects at 5.
- Response size and time limits on every fetch.
- Internal services accept traffic only from known service identities.

## 10. Secrets and cryptography

- In transit: TLS 1.2+ everywhere, TLS 1.3 preferred; HSTS on all web origins.
- At rest: managed disk/DB encryption, plus field-level AES-256-GCM for S2, plus envelope encryption (per-tenant
  data keys wrapped by a KMS key) for S2/S3. Key rotation yearly and on incident.
- Secrets in a secret manager per environment (dev/staging/prod), each service with only its own secrets.
  `.env` is for local development only and never committed (gitleaks pre-commit + CI scanning).
- Prefer OAuth/OIDC over stored passwords. When a site password must exist (e.g. Workday accounts), prefer
  the user's own password manager/passkeys on the device; if ARIA must store one, only in the Credential
  Broker, encrypted, with per-use audit and user-visible inventory.
- No custom crypto. Use vetted libraries (`cryptography`, cloud KMS).

## 11. Authentication and sessions (NIST SP 800-63-4)

- Managed OIDC provider; passkeys preferred; MFA required for all accounts with stored job-site credentials or
  autopilot enabled; phishing-resistant MFA for admin accounts.
- Web sessions: `Secure; HttpOnly; SameSite=Lax` (Strict for admin), short idle timeout, rotation on login,
  server-side revocation. Job-site session cookies are never sent to the frontend.
- Re-authentication for sensitive actions: viewing S2 data, changing policies to autopilot, exporting data,
  deleting the account, adding a device.

## 12. API security (OWASP API Top 10)

- Object-level authorization on every request (tenant + ownership), enforced in the data layer with
  Postgres RLS **and** in code; automated tests try cross-tenant access on every endpoint.
- Schema validation on all inputs (Pydantic, strict mode); explicit response models (no mass assignment, no
  over-returning fields).
- Rate limits (per user, IP, device, endpoint): login 5/min, password reset 3/hour, general API by tier;
  agent limits: applications/hour and /day, concurrent runner sessions per user, LLM requests and tokens per
  user per day, email processing, uploads. Limits protect both security and the LLM bill.
- Pagination limits, request size limits, idempotency keys for state-changing calls.
- Versioned API, deprecations announced; inventory of every endpoint in OpenAPI.

## 13. LLM gateway and third-party AI providers

- The gateway is the only component with provider keys. It enforces data-class rules (no S2/S3), redacts
  S1 where the task doesn't need it, logs metadata (not content) by default, tracks cost per user and
  per application, and supports provider failover.
- **Data minimization per task.** "Why are you interested in this role?" gets: relevant facts, motivations,
  the JD, company facts. Not: address, phone, visa status, EEO, tokens, the whole graph, the inbox.
- Provider due diligence before use, recorded in DECISIONS.md: retention period, training use (must be off),
  processing region, logging, zero-data-retention options, contractual terms (DPA).
- Local models (embeddings, NLI) for anything that would otherwise send whole candidate profiles out.

## 14. Web application baseline

`Content-Security-Policy` (nonce-based, no `unsafe-inline`), `Strict-Transport-Security`,
`X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy`
(deny camera, mic, geolocation unless needed), `frame-ancestors 'none'`, CSRF protection for cookie-auth
endpoints, output encoding everywhere, no `dangerouslySetInnerHTML` with untrusted data.

## 15. Supply chain (OWASP Top 10:2025 A03)

Lockfiles with hashes (`uv.lock`, `pnpm-lock.yaml`); minimal dependencies; pinned versions for critical
packages (browser automation, crypto, auth); Renovate/Dependabot; SCA (pip-audit/osv-scanner, npm audit);
Semgrep + CodeQL; container image scanning; SBOM per release (CycloneDX); signed releases for the Runner;
GitHub Actions pinned by commit SHA with least-privilege tokens. New dependencies are reviewed before
adding: maintainers, activity, install scripts. Browser-agent packages are vetted before use, never adopted blindly.

## 16. Audit, monitoring and incident response

- Append-only, tamper-evident audit log (hash-chained) of every agent action, policy decision, data access to
  S2, credential use, admin action and auth event. Users can see their own audit trail.
- Alerts on anomalies: spikes in failed logins, cross-tenant denials, policy blocks, LLM spend, runner errors.
- Incident response plan: severity levels, contacts, user notification templates, key-rotation runbook.
  Drill twice a year.
- Backups encrypted; restore tested quarterly.

## 17. Privacy and data lifecycle (NIST Privacy Framework)

- Explicit consent per sensitive class (visa, EEO). Users can answer "decline" for EEO and ARIA respects it.
- Privacy policy in plain language: what we collect, why, which providers process it, retention.
- Retention: job postings 180 days after close; receipts and applications for the account's lifetime or as
  the user sets; raw emails 30 days, then only the classification; uploads' originals deleted after
  processing.
- Self-service export (machine-readable) and deletion (including backups within the backup window).
- Applicable laws to review with counsel before public launch: CCPA/CPRA and other US state privacy laws;
  GDPR if EU users are accepted.

## 18. Staged checklist

**Stage A — design-in from day one (hard to retrofit):** tenant model + RLS + cross-tenant tests;
data classification + field encryption for S2; Credential Broker boundary; LLM gateway with data-class
enforcement; policy engine + capabilities; untrusted-content separation + two-model pattern; audit log;
local Runner architecture; secrets never in code; pre-commit secret scanning; dependency lockfiles.

**Stage B — before private beta:** managed auth + MFA/passkeys; rate limits; upload pipeline; security headers;
SCA/SAST in CI; prompt-injection eval set; privacy policy; export/delete; incident plan v1; backups + restore test.

**Stage C — before public launch:** external penetration test; threat-model review; SBOM + signed releases;
email OAuth assessment if used; legal review (ToS of job sites, privacy laws, liability); incident drill;
ASVS L2 self-assessment documented.
