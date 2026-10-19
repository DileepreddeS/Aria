"""Run an eval suite: ``uv run python -m evals.run resume-synthetic [--models] [--update-baseline]``.

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
from typing import Any

from evals.resume.suite import BASELINES, NOTE, REPORTS, SUITES, Report, baseline_view, run_suite


def _table(report: Report) -> str:
    columns = (
        "posting", "truth", "banned", "one page", "layout", "ATS", "labels",
        "coverage", "fit", "rounds", "recruiter (reported)",
    )  # fmt: skip
    rows = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for posting in report.postings:
        gates, reported = posting.gates, posting.reported
        recruiter = (
            ", ".join(f"{name} {count}" for name, count in reported["recruiter_findings"].items()) or "none"
        )
        labels = gates.get("label_mismatches")
        rows.append(
            f"| {posting.name} | {len(gates['truth_violations'])} | {len(gates['banned_words'])} | "
            f"{'yes' if gates['one_page'] else 'NO'} | {len(gates['layout_findings'])} | "
            f"{len(gates['ats_findings'])} | "
            f"{'n/a' if labels is None else len(labels)} | {reported['coverage']:.2f} | {reported['fit']} | "
            f"{reported['fit_rounds']} | {recruiter} |"
        )
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an eval suite.")
    parser.add_argument("suite", choices=sorted(SUITES))
    parser.add_argument(
        "--models", action="store_true", help="use the local embedding model (./tasks.ps1 models)"
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="rewrite the committed baseline (synthetic suite, CI configuration)",
    )
    args = parser.parse_args(argv)

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
    report = run_suite(args.suite, embedder=embedder)
    data: dict[str, Any] = report.as_dict()

    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    written = REPORTS / f"{args.suite}{'-models' if args.models else ''}-{stamp}.json"
    written.write_text(
        json.dumps({**data, "written_at": stamp}, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    print(f"{args.suite}: {NOTE}")
    print(f"configuration: {json.dumps(report.configuration, sort_keys=True)}\n")
    print(_table(report))
    print(f"\nreport: {written}")
    for failure in report.failures:
        print(f"GATE FAILED  {failure}")
    status = 0 if not report.failures else 1

    baseline = BASELINES.get(args.suite)
    if baseline is not None and not args.models:
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
