"""``aria`` — the command line, while there is no dashboard.

Written with argparse rather than a framework: the whole surface is a dozen verbs
over one HTTP client, and a dependency would be carried for the help text alone.

The output is plain text, aligned, with the thing you need to type next on the line.
A review queue is only useful if the id you have to paste is readable, so ids print
in full and the text is truncated instead — it is the text you can see on screen and
the id you cannot retype from memory.

Exit codes: 0 success, 1 the API refused (with its reason printed), 2 usage.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from aria_cli.client import ApiError, AriaClient

__all__ = ["main"]

#: Where the CLI expects the API, matching the default bind in Settings.
DEFAULT_URL = "http://127.0.0.1:8080"


def _from_env_file() -> tuple[str, str]:
    """``(url, token)`` from .env, for when the environment does not say.

    The API reads .env already; having the CLI ignore it would mean setting a
    variable that is written down three feet away. Settings also validates the
    database URL, which the CLI does not need, so a missing or half-written .env is
    not an error here — it just means no default.
    """
    try:
        from aria_core.config import Settings

        settings = Settings()
    except Exception:
        return "", ""

    token = settings.api_service_token
    return (
        f"http://{settings.api_bind_host}:{settings.api_bind_port}",
        token.get_secret_value() if token else "",
    )


#: How much of a fact's text to show in a list. Long enough to recognise the claim.
_TEXT_WIDTH = 92


def _print_lines(payload: dict[str, Any], key: str = "summary") -> None:
    for line in payload.get(key, []):
        print(line)


def _truncate(text: str, width: int = _TEXT_WIDTH) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= width else collapsed[: width - 1] + "\u2026"


# ---------------------------------------------------------------------- commands


def _cmd_health(client: AriaClient, args: argparse.Namespace) -> int:
    payload = client.health()
    print(f"api: {payload.get('status')} ({payload.get('env')})")
    return 0


def _cmd_upload(client: AriaClient, args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.is_file():
        print(f"no such file: {path}", file=sys.stderr)
        return 2

    result = client.upload(path, keep_original=args.keep_original)
    if result.get("refusal_reason"):
        # A refusal is the expected answer for a file ARIA will not take, so it is
        # reported as one rather than as a failure of the command.
        print(f"refused ({result['refusal_reason']}): {result.get('refusal_advice', '')}")
        print(f"upload {result['upload_id']}")
        return 1

    print(f"upload {result['upload_id']}")
    if result.get("duplicate_of"):
        print("  (you already uploaded this file; nothing was reprocessed)")
    print(f"  type       {result.get('detected_type')}")
    print(f"  extractor  {result.get('extractor')}  ({result.get('characters', 0):,} characters)")
    for link in result.get("links", []):
        print(f"  link       {link}")
    columns = result.get("column_pages") or []
    if columns:
        pages = ", ".join(str(number) for number in columns)
        print(f"  COLUMNS    page {pages} was laid out in columns; text rebuilt in reading order")
    if result.get("needs_review"):
        print("  NEEDS REVIEW: check the imported facts against the original")
    print(f"  detail     {result.get('detail', '')}")

    if args.and_import:
        print()
        return _import(client, result["upload_id"], as_notes=args.as_notes)
    print(f"\nnext: aria import {result['upload_id']}")
    return 0


def _import(client: AriaClient, upload_id: str, *, as_notes: bool) -> int:
    payload = client.import_upload(upload_id, as_notes=as_notes)
    if payload.get("status") == "needs_user_fix":
        # Refused as a whole: nothing was written, and the reasons say what to fix.
        print("NOT IMPORTED: nothing was written to your graph.")
        for reason in payload.get("reasons", []):
            print(f"  - {reason}")
        for dropped in payload.get("dropped", []):
            print(f"  dropped    {dropped}")
        print(f"\nsee how ARIA read it: aria lines {upload_id}")
        return 1
    _print_lines(payload)
    for dropped in payload.get("dropped", []):
        print(f"  dropped    {dropped}")
    for flag in payload.get("flags", []):
        print(f"  CHECK      {flag}")
    queued = payload.get("questions_queued", 0)
    if queued:
        print(f"\n{queued} question(s) waiting: aria questions")
    return 0


def _cmd_import(client: AriaClient, args: argparse.Namespace) -> int:
    return _import(client, args.upload_id, as_notes=args.as_notes)


def _cmd_lines(client: AriaClient, args: argparse.Namespace) -> int:
    payload = client.lines(args.upload_id)
    for line in payload.get("lines", []):
        dates = f"  [{line['date_text']}]" if line.get("date_text") else ""
        print(
            f"{line['number']:4}  p{line['page']} c{line['column']}  {line['kind']:<14} "
            f"{_truncate(line['text'], 72)}{dates}"
        )
    ambiguous = payload.get("ambiguous_pages") or []
    if ambiguous:
        pages = ", ".join(str(number) for number in ambiguous)
        print(f"\ncolumn order not settled on page {pages}: this is why the import was refused")
    return 0


def _cmd_facts(client: AriaClient, args: argparse.Namespace) -> int:
    payload = client.facts(status=args.status)
    facts = payload.get("facts", [])
    if not facts:
        print(f"no {args.status} facts")
        return 0

    print(f"{payload.get('total', len(facts))} {args.status} fact(s)")
    for fact in facts:
        where = f"line {fact['source_line']}" if fact.get("source_line") else "no line"
        print(f"\n  {fact['fact_id']}  ({where}, v{fact['version']}, {fact['source']})")
        print(f"    {_truncate(fact['text'])}")
    if args.status == "draft":
        print('\nnext: aria approve <id> | aria reject <id> | aria edit <id> "new text"')
    return 0


ENTITY_KINDS = (
    "experience",
    "project",
    "education",
    "profile_field",
    "declared_skill",
    "achievement",
    "motivation",
)


def _cmd_entities(client: AriaClient, args: argparse.Namespace) -> int:
    payload = client.entities(args.kind, status=args.status)
    rows = payload.get("entities", [])
    if not rows:
        print(f"no {args.status} {args.kind}(s)")
        return 0

    print(f"{payload.get('total', len(rows))} {args.status} {args.kind}(s)")
    for row in rows:
        where = f"line {row['source_line']}" if row.get("source_line") else row.get("source", "")
        print(f"\n  {row['entity_id']}  ({where})")
        print(f"    {_truncate(row['summary'])}")
    if args.status == "draft":
        print(f"\nnext: aria approve <id> --kind {args.kind} | aria reject <id> --kind {args.kind}")
    return 0


def _cmd_approve(client: AriaClient, args: argparse.Namespace) -> int:
    if args.kind == "fact":
        fact = client.approve(args.fact_id)
        print(f"approved: {_truncate(fact['text'])}")
    else:
        row = client.approve_entity(args.kind, args.fact_id)
        print(f"approved {args.kind}: {_truncate(row['summary'])}")
    return 0


def _cmd_reject(client: AriaClient, args: argparse.Namespace) -> int:
    if args.kind == "fact":
        fact = client.reject(args.fact_id, reason=args.reason)
        print(f"rejected: {_truncate(fact['text'])}")
    else:
        row = client.reject_entity(args.kind, args.fact_id, reason=args.reason)
        print(f"rejected {args.kind}: {_truncate(row['summary'])}")
    print("(kept, so an import will not propose it again)")
    return 0


def _cmd_edit(client: AriaClient, args: argparse.Namespace) -> int:
    """Rewriting a fact appends a revision; the earlier wording is kept.

    Only facts can be edited. A role or a degree is a set of fields rather than a
    sentence, so correcting one means rejecting it and importing or answering again,
    not rewriting a line of text.
    """
    fact = client.edit(args.fact_id, args.text, approve=not args.keep_draft)
    print(f"v{fact['version']} ({fact['status']}): {_truncate(fact['text'])}")
    return 0


def _cmd_reconsider(client: AriaClient, args: argparse.Namespace) -> int:
    if args.kind == "fact":
        fact = client.reconsider(args.fact_id)
        print(f"back to draft: {_truncate(fact['text'])}")
    else:
        row = client.reconsider_entity(args.kind, args.fact_id)
        print(f"back to draft ({args.kind}): {_truncate(row['summary'])}")
    return 0


def _cmd_merge(client: AriaClient, args: argparse.Namespace) -> int:
    fact = client.merge(args.fact_id, args.keep)
    print(f"merged. kept: {_truncate(fact['text'])}")
    return 0


def _cmd_questions(client: AriaClient, args: argparse.Namespace) -> int:
    payload = client.questions(status=args.status)
    questions = payload.get("questions", [])
    if not questions:
        print(f"no {args.status} questions")
        return 0

    print(f"{len(questions)} {args.status} question(s)")
    for question in questions:
        lines = question.get("source_lines") or []
        where = f" (line{'s' if len(lines) != 1 else ''} {', '.join(map(str, lines))})" if lines else ""
        print(f"\n  {question['question_id']}  [{question['kind']}]{where}")
        print(f"    {question['prompt']}")
        if question.get("detail"):
            print(f"    {_truncate(question['detail'], 100)}")
        if question.get("answer"):
            print(f"    answered: {question['answer']}")
    if args.status == "open":
        print('\nnext: aria answer <id> "2023-06 to 2023-08"   |   aria dismiss <id>')
    return 0


def _cmd_answer(client: AriaClient, args: argparse.Namespace) -> int:
    payload = client.answer(args.question_id, args.answer)
    print(payload.get("summary", "recorded"))
    if payload.get("fact_ids"):
        print(f"{len(payload['fact_ids'])} draft fact(s) came with it: aria facts")
    return 0


def _cmd_dismiss(client: AriaClient, args: argparse.Namespace) -> int:
    question = client.dismiss(args.question_id, reason=args.reason)
    print(f"dismissed: {question['prompt']}")
    return 0


def _cmd_gaps(client: AriaClient, args: argparse.Namespace) -> int:
    payload = client.gaps()
    found = payload.get("gaps", [])
    if not found:
        print("no gaps ARIA knows how to ask about")
        return 0

    print(f"{payload.get('total', len(found))} gap(s), most useful first")
    for gap in found:
        print(f"\n  [{gap['kind']}]")
        print(f"    {_truncate(gap['prompt'], 100)}")
    print("\nnext: aria interview enrichment   (asks the most useful few)")
    return 0


def _cmd_interview(client: AriaClient, args: argparse.Namespace) -> int:
    payload = client.motivations() if args.which == "motivations" else client.enrichment(args.limit)
    print(payload.get("summary", ""))
    for prompt in payload.get("prompts", []):
        print(f"  ? {_truncate(prompt, 100)}")
    if payload.get("queued"):
        print("\nnext: aria questions   |   aria answer <id> ...")
    return 0


def _cmd_report(client: AriaClient, args: argparse.Namespace) -> int:
    payload = client.completeness()
    _print_lines(payload)
    # Exit 1 when the graph is not complete, so the command is usable in a script.
    return 0 if payload.get("complete") else 1


# ----------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aria",
        description="ARIA's command line. Talks to the local API; never to the database.",
    )
    # The environment wins, then .env, then the built-in default. That order makes
    # pointing at a second identity a matter of one exported variable.
    env_url, env_token = _from_env_file()
    parser.add_argument(
        "--url",
        default=os.environ.get("ARIA_API_URL") or env_url or DEFAULT_URL,
        help=f"API base URL (default: ARIA_API_URL, then .env, then {DEFAULT_URL})",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("ARIA_API_SERVICE_TOKEN") or env_token,
        help="API token (default: ARIA_API_SERVICE_TOKEN, then .env)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("health", help="check the API is up").set_defaults(handler=_cmd_health)

    upload = subparsers.add_parser("upload", help="upload a resume (PDF or text)")
    upload.add_argument("path")
    upload.add_argument(
        "--keep-original",
        action="store_true",
        help="keep the original file in quarantine (it is deleted by default)",
    )
    upload.add_argument("--and-import", action="store_true", help="import it straight away")
    upload.add_argument("--as-notes", action="store_true", help="read it as a skills list")
    upload.set_defaults(handler=_cmd_upload)

    importer = subparsers.add_parser("import", help="import an upload into the graph as drafts")
    importer.add_argument("upload_id")
    importer.add_argument("--as-notes", action="store_true", help="read it as a skills list")
    importer.set_defaults(handler=_cmd_import)

    lines = subparsers.add_parser("lines", help="show how ARIA read a PDF upload, line by line")
    lines.add_argument("upload_id")
    lines.set_defaults(handler=_cmd_lines)

    facts = subparsers.add_parser("facts", help="list facts (default: the review queue)")
    facts.add_argument("--status", default="draft", choices=["draft", "approved", "rejected"])
    facts.set_defaults(handler=_cmd_facts)

    approve = subparsers.add_parser("approve", help="approve something \u2014 this makes it a claim")
    approve.add_argument("fact_id", metavar="id")
    approve.add_argument(
        "--kind",
        default="fact",
        choices=["fact", *ENTITY_KINDS],
        help="what the id refers to (default: fact)",
    )
    approve.set_defaults(handler=_cmd_approve)

    reject = subparsers.add_parser("reject", help="reject something")
    reject.add_argument("fact_id", metavar="id")
    reject.add_argument("--reason", default="")
    reject.add_argument("--kind", default="fact", choices=["fact", *ENTITY_KINDS])
    reject.set_defaults(handler=_cmd_reject)

    edit = subparsers.add_parser("edit", help="rewrite a fact (appends a revision)")
    edit.add_argument("fact_id")
    edit.add_argument("text")
    edit.add_argument("--keep-draft", action="store_true", help="leave it a draft instead of approving it")
    edit.set_defaults(handler=_cmd_edit)

    reconsider = subparsers.add_parser("reconsider", help="move something rejected back to draft")
    reconsider.add_argument("fact_id", metavar="id")
    reconsider.add_argument("--kind", default="fact", choices=["fact", *ENTITY_KINDS])
    reconsider.set_defaults(handler=_cmd_reconsider)

    merge = subparsers.add_parser("merge", help="fold one fact into another")
    merge.add_argument("fact_id")
    merge.add_argument("keep", help="the fact to keep")
    merge.set_defaults(handler=_cmd_merge)

    questions = subparsers.add_parser("questions", help="things ARIA will not guess")
    questions.add_argument("--status", default="open", choices=["open", "answered", "dismissed"])
    questions.set_defaults(handler=_cmd_questions)

    answer = subparsers.add_parser("answer", help="answer a question")
    answer.add_argument("question_id")
    answer.add_argument("answer")
    answer.set_defaults(handler=_cmd_answer)

    dismiss = subparsers.add_parser("dismiss", help="set a question aside")
    dismiss.add_argument("question_id")
    dismiss.add_argument("--reason", default="")
    dismiss.set_defaults(handler=_cmd_dismiss)

    entities = subparsers.add_parser(
        "entities", help="list roles, degrees, projects, skills, contact details, motivations"
    )
    entities.add_argument("kind", choices=ENTITY_KINDS)
    entities.add_argument("--status", default="draft", choices=["draft", "approved", "rejected"])
    entities.set_defaults(handler=_cmd_entities)

    subparsers.add_parser("gaps", help="what is missing from the graph (read-only)").set_defaults(
        handler=_cmd_gaps
    )

    interview = subparsers.add_parser(
        "interview", help="ask about gaps (enrichment) or about what you want (motivations)"
    )
    interview.add_argument("which", choices=["enrichment", "motivations"])
    interview.add_argument(
        "--limit",
        type=int,
        default=None,
        help="how many questions to queue (enrichment only; few on purpose)",
    )
    interview.set_defaults(handler=_cmd_interview)

    subparsers.add_parser("report", help="is the graph complete? exits 1 when it is not").set_defaults(
        handler=_cmd_report
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.token:
        print(
            "no API token. Put ARIA_API_SERVICE_TOKEN in .env (see .env.example), "
            "set it in your shell, or pass --token.",
            file=sys.stderr,
        )
        return 2

    try:
        with AriaClient(args.url, args.token) as client:
            return int(args.handler(client, args))
    except ApiError as error:
        print(f"error: {error.detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
