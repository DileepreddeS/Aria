<#
    ARIA local task runner (Windows).

    Usage:  ./tasks.ps1 <task>
    Tasks:  setup, up, down, kms-init, keys-init, migrate, anchor, test, test-db, lint,
            fmt, typecheck, control-chars, pii, secrets, audit, check, gateway, api, seed-me,
            parser-image

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
        Invoke-Step "mypy (document_parser)" { uv run mypy services/document_parser }
        Invoke-Step "mypy (api)" { uv run mypy apps/api }
        Invoke-Step "mypy (cli)" { uv run mypy apps/cli }
        Invoke-Step "mypy (resume)" { uv run mypy engines/resume }
    }
    "control-chars" {
        Invoke-Step "no stray control characters" { uv run python infra/checks/no_control_characters.py }
    }
    "pii" {
        Invoke-Step "no real personal identifiers" { uv run python infra/checks/no_personal_identifiers.py }
    }
    "secrets" {
        Invoke-Step "gitleaks" { gitleaks dir . --redact --verbose }
    }
    "audit" {
        Invoke-Step "pip-audit" { uv run pip-audit }
        Invoke-Step "dependency age (14 days, CVE fixes exempt)" { uv run python infra/checks/dependency_age.py }
    }
    "check" {
        & $PSCommandPath lint
        & $PSCommandPath typecheck
        & $PSCommandPath control-chars
        & $PSCommandPath pii
        & $PSCommandPath test-db
        & $PSCommandPath secrets
    }
    "gateway" {
        Invoke-Step "llm_gateway (loopback only)" { uv run python -m aria_llm_gateway.main }
    }
    "api" {
        # Through the module, so the loopback bind and the start-up checks come from
        # configuration rather than from flags typed here.
        Invoke-Step "api (loopback only)" { uv run python -m aria_api.main }
    }
    "seed-me" {
        Invoke-Step "create the tenant, user and candidate the API acts as" {
            uv run python -m aria_api.seed @Rest
        }
    }
    "parser-image" {
        # Uploads are parsed in this container. Rebuild after any change to the parser
        # or to aria_core: the API compares the image's fingerprint with the checkout
        # and will not start on a stale one.
        Invoke-Step "build the document parser image" { uv run python -m aria_document_parser.image build }
    }
    default {
        Get-Help $PSCommandPath -Detailed
    }
}
