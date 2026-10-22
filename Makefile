# CI task definitions. Local development on Windows uses ./tasks.ps1.
.PHONY: install lint typecheck test eval secrets sast audit control-chars pii parser-image browsers models ci

install:
	uv sync --all-packages --frozen

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	# One invocation per workspace member: each has its own tests package, and mypy
	# cannot hold two modules of the same name in one run.
	uv run mypy packages/core
	uv run mypy services/llm_gateway
	uv run mypy services/document_parser
	uv run mypy apps/api
	uv run mypy apps/cli
	uv run mypy engines/resume
	uv run mypy evals

test:
	uv run pytest

# The resume eval on the synthetic postings, in the CI configuration (the fake embedder): its
# gates fail the run, and so does any difference from the committed baseline. Needs Chromium.
eval:
	uv run python -m evals.run resume-synthetic

# The container uploads are parsed in. The docker-marked tests need it, and in CI a
# missing image fails them rather than skipping them.
parser-image:
	uv run python -m aria_document_parser.image build

# The Chromium resumes are printed through. The chromium-marked tests need it and fail
# rather than skip without it; --with-deps adds the system libraries a bare runner lacks.
browsers:
	uv run playwright install --with-deps chromium

# The local model weights models.lock pins: commit URLs, SHA-256 checked, nothing at runtime.
# Needs the ml group (uv sync --group ml). CI's unit tests never run it; an eval job does.
models:
	uv run python infra/tools/fetch_models.py

control-chars:
	uv run python infra/checks/no_control_characters.py

# A real person's identifiers anywhere in the repository. The denylist comes from the
# ARIA_PII_DENYLIST secret in CI and private/pii-denylist.txt locally; without one
# the check fails rather than passing unchecked.
pii:
	uv run python infra/checks/no_personal_identifiers.py

secrets:
	gitleaks dir . --redact --verbose --exit-code 1

sast:
	semgrep --config p/python --config p/security-audit --config .semgrep/aria.yml --error --metrics off

# Known vulnerabilities, then the 14-day rule for every pinned file (DECISIONS
# 2026-10-12): a release younger than that is refused unless it is a declared security
# fix, pinned exactly and confirmed by OSV, until its exemption expires (DECISIONS 2026-10-22).
# The whole lock is audited, every group included, not the installed environment:
# CI never installs the optional ml group, and its packages are audited all the same.
audit:
	@tmp=$$(mktemp) && uv export --frozen --all-packages --all-groups --no-emit-workspace -q -o "$$tmp" 		&& uv run pip-audit --require-hashes --disable-pip -r "$$tmp"; status=$$?; rm -f "$$tmp"; exit $$status
	uv run python infra/checks/dependency_age.py

ci: lint typecheck control-chars pii test
