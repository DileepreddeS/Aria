# CI task definitions. Local development on Windows uses ./tasks.ps1.
.PHONY: install lint typecheck test secrets sast audit control-chars pii parser-image ci

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

test:
	uv run pytest

# The container uploads are parsed in. The docker-marked tests need it, and in CI a
# missing image fails them rather than skipping them.
parser-image:
	uv run python -m aria_document_parser.image build

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
# 2026-10-12): a release younger than that is refused unless it fixes a CVE.
audit:
	uv run pip-audit
	uv run python infra/checks/dependency_age.py

ci: lint typecheck control-chars pii test
