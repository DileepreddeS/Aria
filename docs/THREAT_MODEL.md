# ARIA — Threat model (STRIDE)

Companion to `SECURITY.md`. SECURITY.md §1 holds the asset summary; this file holds the per-boundary
STRIDE analysis. Filled in as each component is built — a component is not "done" until its row exists.

Scope of a row: one trust boundary or one component. Each entry records the threat, whether it is
mitigated, by what control, and where the test lives.

## Trust boundaries

| # | Boundary | Status |
|---|---|---|
| B1 | User device (Runner) ↔ cloud API | Phase 5 |
| B2 | API ↔ database | **Phase 0** |
| B3 | API ↔ LLM gateway ↔ providers | **Phase 0** (gateway skeleton; real provider in Phase 2) |
| B4 | Crawler ↔ the internet | Phase 3 |
| B5 | Runner ↔ job sites | Phase 5 |
| B6 | Inbound email ↔ ARIA | Phase 8 |
| B7 | Third-party packages ↔ our code | **Phase 0** (supply chain in CI) |
| B8 | Tenant ↔ tenant | **Phase 0** |

## B8 — Tenant ↔ tenant

| STRIDE | Threat | Mitigation | Test |
|---|---|---|---|
| Information disclosure | One tenant reads another tenant's rows | Postgres RLS on every tenant table with `FORCE ROW LEVEL SECURITY`; `aria_app` has `NOBYPASSRLS` and does not own the tables; the session helper refuses to open a transaction without a scope set | `test_rls_cross_tenant.py` |
| Tampering | `aria_app` disables row security to escape the policy | Table ownership held by `aria_migrate`/`aria_owner`; `ALTER TABLE` requires ownership | `test_rls_cross_tenant.py::TestTheApplicationRoleCannotEscape` |
| Information disclosure | A tenant's encrypted S2 data is decrypted with another tenant's key, or moved between rows | Per-tenant data keys; AES-GCM associated data binds ciphertext to `tenant_id + table + column + row_id` | `test_crypto_envelope.py` |
| Repudiation | Audit history is rewritten to hide an action | Hash-chained events, INSERT-only grants for `aria_app`, per-chain serialized writes | `test_audit_chain.py` |
| Repudiation | History is rewritten *consistently*, so the chain still verifies | Daily head anchors in an append-only store outside the database. Phase 0's store is a local file, which raises the cost rather than removing the risk; production needs a store the application cannot rewrite | `test_audit_anchor.py` |

## B2 — API ↔ database

| STRIDE | Threat | Mitigation | Test |
|---|---|---|---|
| Elevation of privilege | The API role can alter schema or read `pg_authid` | `aria_app` is `NOSUPERUSER NOCREATEDB NOCREATEROLE` and owns nothing | `test_rls_cross_tenant.py` |
| Information disclosure | S2 values readable from a database dump | Field-level AES-256-GCM with per-tenant keys wrapped by KMS; plaintext never stored | `test_crypto_envelope.py`, `test_s2_storage.py` |
| Elevation of privilege | A request-serving process holds the migration role's credentials, which bypass tenant isolation | No field for them on `Settings`; a separate `MigrationSettings` reading `.env.migrate`; a start-up check against the process environment | `api/tests/test_startup.py` |
| Information disclosure | A later feature reads an encrypted column outside the one module that handles it | Columns declare their class and their accessor module; an architecture test fails on any other mention, including in raw SQL | `test_architecture_s2.py` |

## B3 — API ↔ LLM gateway ↔ providers

| STRIDE | Threat | Mitigation | Test |
|---|---|---|---|
| Information disclosure | S2/S3 data sent to a third-party provider | Gateway rejects any S2/S3-annotated field, and scans free text for S2 patterns before dispatch | `llm_gateway/tests/test_gateway.py` |
| Tampering | Untrusted page/JD text is treated as an instruction | Untrusted content travels only in `untrusted_content` blocks; the assembler cannot concatenate it into instructions | `test_gateway.py::TestUntrustedContentCannotBecomeAnInstruction` |
| Spoofing | Another local process calls the gateway | Loopback-only bind + constant-time service-token check | `llm_gateway/tests/test_gateway.py` |
| Repudiation | Provider spend cannot be attributed | Per-request metadata row with tenant, purpose, tokens and cost; no content | `llm_gateway/tests/test_gateway.py` |

## B1 — Runner ↔ cloud API (ApplyTask)

| STRIDE | Threat | Mitigation | Test |
|---|---|---|---|
| Spoofing | A forged task makes the Runner submit an application | Ed25519 signature over the canonical payload, verified by `kid` | `test_apply_task_signing.py` |
| Replay | A captured task is submitted twice | Single-use `jti` recorded on consumption; expiry window | `test_apply_task_signing.py` |
| Tampering | A field is altered in transit | The signature covers the exact transmitted bytes, verified before they are parsed, so there is no canonicalisation gap | `test_apply_task_signing.py` |
| Information disclosure | A captured task is a copy of the candidate's profile | The task carries answer *keys* and a file hash, never values; its strictest field is S1 | `test_apply_task_signing.py::TestTheTaskCarriesReferencesNotValues` |

## B7 — Third-party packages ↔ our code

| STRIDE | Threat | Mitigation | Test |
|---|---|---|---|
| Tampering | A dependency or action is swapped for a malicious version | `uv.lock` with hashes; GitHub Actions pinned by commit SHA; the Postgres image pinned by digest; gitleaks installed from a release checked against its published SHA-256 | CI `secrets`, `dependencies` jobs |
| Tampering | A boundary in this document is crossed by new code | `.semgrep/aria.yml` encodes the boundaries as static rules, verified against a file of planted violations | CI `sast` job |

Remaining boundaries are filled in by their phase.
