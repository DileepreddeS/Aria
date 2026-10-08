# ARIA

A job-search agent. It finds the right jobs, builds an honest tailored resume for each, answers
application questions truthfully, applies through the user's own browser at human pace, proves every
submission and learns what gets interviews.

North-star metric: **interviews per 100 applications.**

Start with `docs/PRODUCT_SPEC.md`. `docs/SECURITY.md` overrides convenience. Decisions are logged in
`docs/DECISIONS.md`.

## Status

Phase 0 — foundations. See PRODUCT_SPEC §7 for the phase plan.

## Local setup (Windows)

Prerequisites: Python is managed by `uv`, so only these are needed:

```powershell
winget install --id=astral-sh.uv -e
winget install --id=Gitleaks.Gitleaks -e     # pre-commit secret scanning
# Docker Desktop for the local Postgres
```

Then:

```powershell
./tasks.ps1 setup        # uv sync + install git hooks
./tasks.ps1 kms-init     # dev KMS root key -> %APPDATA%\aria
./tasks.ps1 keys-init    # ApplyTask Ed25519 keypair -> %APPDATA%\aria
./tasks.ps1 up           # Postgres 16 on 127.0.0.1:5433
./tasks.ps1 migrate      # Alembic
./tasks.ps1 check        # lint + types + tests + secret scan
```

Secrets and keys live in `%APPDATA%\aria` and in `.env` — both outside version control.
`private/` is for the founder's own data when testing locally and is gitignored.

## Layout

| Path | What |
|---|---|
| `packages/core` | typed schemas, data classification, state machines, policy engine, audit log, crypto, DB |
| `services/llm_gateway` | the only component allowed to call LLM providers |
| `apps/api` | cloud API (modular monolith) |
| `infra` | docker-compose, Postgres roles, Alembic |
| `evals` | fixed evaluation suites (from Phase 2) |
| `docs` | spec, security, decisions, threat model |

Directories from PRODUCT_SPEC §6 appear when the phase that needs them starts.

## Commands

Local development uses `./tasks.ps1` (run it with no arguments for the task list). CI uses the `Makefile`.
