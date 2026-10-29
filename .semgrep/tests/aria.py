"""Cases for .semgrep/aria.yml, checked by `semgrep --test` (make sast, ./tasks.ps1 sast).

A `ruleid:` comment says the next line must be reported; an `ok:` comment says it must not.
semgrep's default ignore list skips tests/ directories, so the main scan never reads this file.
This is deliberately bad code, never imported or run.
"""
# ruff: noqa

from __future__ import annotations

import datetime

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker

# ruleid: aria-no-direct-llm-provider
import anthropic

# ok: aria-no-direct-llm-provider
from aria_core.schemas.llm import LlmRequest


def a_session_of_its_own(engine):
    # ruleid: aria-no-session-factory-outside-session-module
    return async_sessionmaker(engine)


def a_tenant_scope_by_hand(session):
    # ruleid: aria-no-raw-set-local-for-tenant
    return session.execute("SET LOCAL aria.tenant_id = 'x'")


# ruleid: aria-no-broad-candidate-getter
def get_candidate_data(candidate_id): ...


# ok: aria-no-broad-candidate-getter
def get_candidate_email(candidate_id): ...


def naive_and_aware():
    # ruleid: aria-no-naive-datetime
    naive = datetime.datetime.now()
    # ok: aria-no-naive-datetime
    aware = datetime.datetime.now(datetime.UTC)
    return naive, aware


def fetches():
    # ruleid: aria-no-insecure-http-request
    httpx.get("http://example.com/")
    # ok: aria-no-insecure-http-request
    httpx.get("https://example.com/")
    # ruleid: aria-no-verify-false
    return httpx.Client(verify=False)
