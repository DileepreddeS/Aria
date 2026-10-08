# CI task definitions. Local development on Windows uses ./tasks.ps1.
.PHONY: install lint typecheck test secrets sast audit ci

install:
	uv sync --all-packages --frozen

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy packages/core services/llm_gateway apps/api

test:
	uv run pytest

secrets:
	gitleaks dir . --redact --verbose --exit-code 1

sast:
	semgrep --config p/python --config p/security-audit --config .semgrep/aria.yml --error --metrics off

audit:
	uv run pip-audit

ci: lint typecheck test
