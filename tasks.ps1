<#
    ARIA local task runner (Windows).

    Usage:  ./tasks.ps1 <task>
    Tasks:  setup, up, down, kms-init, keys-init, migrate, anchor, test, test-db, lint,
            fmt, typecheck, secrets, audit, check, gateway, api

    CI uses the Makefile. This file is for local development on Windows.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Task = "help",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

function Invoke-Step([string]$Name, [scriptblock]$Body) {
    Write-Host "==> $Name" -ForegroundColor Cyan
    & $Body
    if ($LASTEXITCODE -ne 0) { throw "$Name failed with exit code $LASTEXITCODE" }
}

function Get-AriaHome {
    $home = Join-Path $env:APPDATA "aria"
    if (-not (Test-Path $home)) { New-Item -ItemType Directory -Path $home | Out-Null }
    return $home
}

switch ($Task) {
    "setup" {
        Invoke-Step "uv sync" { uv sync --all-packages }
        Invoke-Step "pre-commit install" { uv run pre-commit install }
        Write-Host "Next: ./tasks.ps1 kms-init; ./tasks.ps1 keys-init; ./tasks.ps1 up; ./tasks.ps1 migrate"
    }
    "up" {
        Invoke-Step "postgres up" { docker compose -f infra/docker-compose.yml up -d }
    }
    "down" {
        Invoke-Step "postgres down" { docker compose -f infra/docker-compose.yml down }
    }
    "kms-init" {
        $home = Get-AriaHome
        Invoke-Step "dev KMS root key" { uv run python -m aria_core.crypto.kms_init --path (Join-Path $home "dev-kms-root.key") }
    }
    "keys-init" {
        $home = Get-AriaHome
        Invoke-Step "ApplyTask Ed25519 keypair" { uv run python -m aria_core.apply_task.keys_init --path (Join-Path $home "apply-task-signing.key") --kid "dev-1" }
    }
    "anchor" {
        $home = Get-AriaHome
        Invoke-Step "anchor audit chain heads" { uv run python -m aria_core.audit.record_anchors --path (Join-Path $home "audit-anchors.jsonl") }
    }
    "migrate" {
        Invoke-Step "alembic upgrade head" { uv run alembic -c infra/alembic.ini upgrade head }
    }
    "test" {
        Invoke-Step "pytest (no database)" { uv run pytest -m "not db" @Rest }
    }
    "test-db" {
        Invoke-Step "pytest (all, needs postgres)" { uv run pytest @Rest }
    }
    "lint" {
        Invoke-Step "ruff check" { uv run ruff check . }
        Invoke-Step "ruff format --check" { uv run ruff format --check . }
    }
    "fmt" {
        Invoke-Step "ruff format" { uv run ruff format . }
        Invoke-Step "ruff check --fix" { uv run ruff check --fix . }
    }
    "typecheck" {
        # One invocation per workspace member: each has its own tests package, and
        # mypy cannot hold two modules of the same name in one run.
        Invoke-Step "mypy (core)" { uv run mypy packages/core }
        Invoke-Step "mypy (llm_gateway)" { uv run mypy services/llm_gateway }
        Invoke-Step "mypy (api)" { uv run mypy apps/api }
    }
    "secrets" {
        Invoke-Step "gitleaks" { gitleaks dir . --redact --verbose }
    }
    "audit" {
        Invoke-Step "pip-audit" { uv run pip-audit }
    }
    "check" {
        & $PSCommandPath lint
        & $PSCommandPath typecheck
        & $PSCommandPath test-db
        & $PSCommandPath secrets
    }
    "gateway" {
        Invoke-Step "llm_gateway (loopback only)" { uv run python -m aria_llm_gateway.main }
    }
    "api" {
        Invoke-Step "api" { uv run uvicorn aria_api.main:app --host 127.0.0.1 --port 8080 --reload }
    }
    default {
        Get-Help $PSCommandPath -Detailed
    }
}
