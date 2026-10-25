"""Run an eval suite: ``python -m evals.run <suite> [--models] [--llm --tenant ID] [--update-baseline]``.

Suites: ``resume-synthetic`` (gated in CI), ``resume`` (real postings, reported), and
``security`` (the red-team postings; scripted hostile replies, as CI runs them).

``--llm`` reads each posting with Stage 1's model step through a running gateway
(``./tasks.ps1 gateway``), as the tenant ``--tenant`` names, one run per posting. It costs
money and is never run in CI. What it finds is reported, never gated: label mismatches
for the resume suites, findings for the security suite, and it neither compares with nor
updates the baseline.

Every run writes its report to ``evals/resume/reports/`` (untracked) and prints a table. On
the synthetic suite it is then compared with the committed baseline, and a difference fails
the run until the baseline is updated on purpose: ``--update-baseline`` rewrites it, and only
for the CI configuration (the fake embedder), never from a ``--models`` run, and only when
every gate passes. The baseline is a tracked file that changes only when someone means it to,
so a moved number shows up as a diff in review rather than as noise in every working tree.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import uuid
from typing import Any

from evals.resume.suite import (
    BASELINES,
    NOTE,
    REPORTS,
    SUITES,
    LlmFactory,
    Report,
    baseline_view,
    run_suite,
)


def _table(report: Report) -> str:
    columns = (
        "posting", "status", "truth", "banned", "recruiter", "one page", "layout", "ATS", "labels",
        "coverage", "fit", "rounds", "writer (first/rewritten/swapped/fallback)",
    )  # fmt: skip
    rows = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for posting in report.postings:
        gates, reported = posting.gates, posting.reported
        labels = gates.get("label_mismatches")
        writer = reported.get("writer")
        written = (
            "facts' own words"
            if writer is None
            else f"{writer['first_try']}/{writer['rewritten']}/{writer['swapped']}/{writer['fallback']}"
        )
        rows.append(
            f"| {posting.name} | {reported['status']} | {len(gates['truth_violations'])} | "
            f"{len(gates['banned_words'])} | {len(gates['recruiter_findings'])} | "
            f"{'yes' if gates['one_page'] else 'NO'} | {len(gates['layout_findings'])} | "
            f"{len(gates['ats_findings'])} | "
            f"{'n/a' if labels is None else len(labels)} | {reported['coverage']:.2f} | {reported['fit']} | "
            f"{reported['fit_rounds']} | {written} |"
        )
    return "\n".join(rows)


def _gateway_llm(tenant: uuid.UUID) -> LlmFactory:
    """One gateway client per posting, each its own run, so each is capped on its own."""
    from aria_core.config import get_settings
    from aria_core.llm_client import GatewayLlmClient

    settings = get_settings()
    if settings.llm_gateway_service_token is None:
        raise SystemExit("error: --llm needs ARIA_LLM_GATEWAY_SERVICE_TOKEN and a running gateway")
    token = settings.llm_gateway_service_token

    def build(posting: str) -> GatewayLlmClient:
        return GatewayLlmClient(
            base_url=settings.llm_gateway_url, service_token=token, tenant_id=tenant, run_id=uuid.uuid4()
        )

    return build


def _security(llm: LlmFactory | None) -> int:
    import asyncio

    from aria_resume.llm import LlmPurpose, ScriptedLlm

    from aria_core.taxonomy.loader import load_default_taxonomy
    from evals.security.suite import examine, load_postings

    taxonomy = load_default_taxonomy()
    found = 0
    mode = "the real model (reported, never gated)" if llm else "scripted hostile replies (gated, as in CI)"
    print(f"security: red-team postings, read with {mode}\n")
    for posting in load_postings():
        client = llm(posting.name) if llm else ScriptedLlm({LlmPurpose.PARSE_JD: [posting.hostile_json()]})
        examined = asyncio.run(examine(posting, taxonomy, client))
        found += len(examined.findings)
        print(f"{posting.name} ({posting.author}): flags {sorted(examined.flags)}")
        for finding in examined.findings:
            print(f"  {'REPORTED' if llm else 'FAILED'}  {finding}")
        for note in examined.parsed.reading_notes:
            print(f"  note: {note}")
    return 1 if found and llm is None else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an eval suite.")
    parser.add_argument("suite", choices=sorted([*SUITES, "security"]))
    parser.add_argument(
        "--models", action="store_true", help="use the local embedding model (./tasks.ps1 models)"
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="rewrite the committed baseline (synthetic suite, CI configuration)",
    )
    parser.add_argument(
        "--llm",
        action="store_true",
        help="read postings with the model step through a running gateway (costs money)",
    )
    parser.add_argument("--tenant", type=uuid.UUID, help="the tenant the gateway charges, with --llm")
    args = parser.parse_args(argv)

    if args.llm and args.tenant is None:
        print("error: --llm needs --tenant: the gateway caps and records spend per tenant", file=sys.stderr)
        return 2
    if args.update_baseline and args.llm:
        print("error: the baseline is never written from a model run", file=sys.stderr)
        return 2
    llm = _gateway_llm(args.tenant) if args.llm else None
    if args.suite == "security":
        return _security(llm)

    if args.update_baseline and (args.models or args.suite not in BASELINES):
        print(
            "error: the baseline is the synthetic suite in the CI configuration; no --models, no other suite",
            file=sys.stderr,
        )
        return 2

    embedder = None
    if args.models:
        from aria_resume.match.onnx_embedder import OnnxEmbedder

        embedder = OnnxEmbedder()
    report = run_suite(args.suite, embedder=embedder, llm=llm)
    data: dict[str, Any] = report.as_dict()

    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    written = (
        REPORTS / f"{args.suite}{'-models' if args.models else ''}{'-llm' if args.llm else ''}-{stamp}.json"
    )
    written.write_text(
        json.dumps({**data, "written_at": stamp}, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    print(f"{args.suite}: {NOTE}")
    print(f"configuration: {json.dumps(report.configuration, sort_keys=True)}\n")
    print(_table(report))
    for posting in report.postings:
        for gap in posting.reported.get("taxonomy_gaps", []):
            print(f"taxonomy gap  {posting.name}: {gap}")
    print(f"\nreport: {written}")
    for failure in report.failures:
        print(f"GATE FAILED  {failure}")
    status = 0 if not report.failures else 1

    baseline = BASELINES.get(args.suite)
    if baseline is not None and not args.models and not args.llm:
        current = json.dumps(baseline_view(data), indent=2, sort_keys=True) + "\n"
        if args.update_baseline:
            if report.failures:
                print("error: not updating the baseline while a gate fails", file=sys.stderr)
                return 1
            baseline.parent.mkdir(parents=True, exist_ok=True)
            baseline.write_text(current, encoding="utf-8", newline="\n")
            print(f"baseline updated: {baseline}")
        elif not baseline.exists() or baseline.read_text(encoding="utf-8") != current:
            print(
                f"BASELINE DIFFERS  {baseline} does not match this run: if the change is meant, "
                "run with --update-baseline and commit the diff"
            )
            status = status or 1
    return status


if __name__ == "__main__":
    raise SystemExit(main())
