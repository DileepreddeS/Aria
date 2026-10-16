"""The CLI holds no logic, and these tests are about keeping it that way.

Every command is one request against a mock transport, so what is being checked is
the wiring: the right method, the right path, the right body, and output a person can
act on. There is deliberately **no test here that asserts a rule** — no "approving a
rejected fact fails" — because that rule lives in the API, and a copy of it here
would be the beginning of a second implementation.

What does get asserted: an unreachable API says so rather than raising a traceback,
a refusal prints the API's own sentence, and ``report`` exits non-zero when the graph
is not complete, so it is usable in a script.
"""

from __future__ import annotations

import json
import socket
import uuid
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

from aria_cli.client import ApiError, AriaClient
from aria_cli.main import DEFAULT_URL, build_parser, main

TOKEN = "test-token-not-a-secret"
FACT_ID = str(uuid.uuid4())
QUESTION_ID = str(uuid.uuid4())
UPLOAD_ID = str(uuid.uuid4())


class Recorder:
    """Captures requests and replays canned responses."""

    def __init__(self, routes: dict[tuple[str, str], Any]) -> None:
        self.routes = routes
        self.seen: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        key = (request.method, request.url.path)
        payload = self.routes.get(key)
        if payload is None:
            return httpx.Response(404, json={"detail": f"no route for {key}"})
        if isinstance(payload, httpx.Response):
            return payload
        return httpx.Response(200, json=payload)

    def client(self) -> AriaClient:
        client = AriaClient("http://testserver", TOKEN)
        client._client = httpx.Client(
            transport=httpx.MockTransport(self.handler),
            base_url="http://testserver",
            headers=client._client.headers,
        )
        return client


def run(recorder: Recorder, argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    """Run a command against the recorder.

    ``--token`` is a global option, so argparse wants it before the subcommand.
    """
    monkeypatch.setattr("aria_cli.main.AriaClient", lambda *_args, **_kwargs: recorder.client())
    return int(main(["--token", TOKEN, *argv]))


class TestTheClientSendsTheRightRequest:
    def test_approve(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        recorder = Recorder(
            {
                ("POST", f"/facts/{FACT_ID}/approve"): {
                    "fact_id": FACT_ID,
                    "text": "Built 20+ React components",
                    "version": 1,
                    "status": "approved",
                    "source": "resume_import",
                }
            }
        )
        assert run(recorder, ["approve", FACT_ID], monkeypatch) == 0
        assert "approved: Built 20+ React components" in capsys.readouterr().out

    def test_reject_sends_the_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = Recorder(
            {
                ("POST", f"/facts/{FACT_ID}/reject"): {
                    "fact_id": FACT_ID,
                    "text": "Something wrong",
                    "version": 1,
                    "status": "rejected",
                    "source": "resume_import",
                }
            }
        )
        assert run(recorder, ["reject", FACT_ID, "--reason", "not mine"], monkeypatch) == 0
        assert json.loads(recorder.seen[-1].content)["reason"] == "not mine"

    def test_edit_approves_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = Recorder(
            {
                ("POST", f"/facts/{FACT_ID}/edit"): {
                    "fact_id": FACT_ID,
                    "text": "Built 22 components",
                    "version": 2,
                    "status": "approved",
                    "source": "user_edit",
                }
            }
        )
        assert run(recorder, ["edit", FACT_ID, "Built 22 components"], monkeypatch) == 0
        assert json.loads(recorder.seen[-1].content)["approve"] is True

    def test_edit_can_keep_it_a_draft(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = Recorder(
            {
                ("POST", f"/facts/{FACT_ID}/edit"): {
                    "fact_id": FACT_ID,
                    "text": "Built 22 components",
                    "version": 2,
                    "status": "draft",
                    "source": "user_edit",
                }
            }
        )
        assert run(recorder, ["edit", FACT_ID, "Built 22 components", "--keep-draft"], monkeypatch) == 0
        assert json.loads(recorder.seen[-1].content)["approve"] is False

    def test_answer(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        recorder = Recorder(
            {
                ("POST", f"/questions/{QUESTION_ID}/answer"): {
                    "question_id": QUESTION_ID,
                    "subject_kind": "experience",
                    "subject_id": str(uuid.uuid4()),
                    "fact_ids": [str(uuid.uuid4()), str(uuid.uuid4())],
                    "detail": "Jun 2023 to Aug 2023",
                    "summary": "created experience ... as a draft for you to approve",
                }
            }
        )
        assert run(recorder, ["answer", QUESTION_ID, "2023-06 to 2023-08"], monkeypatch) == 0
        out = capsys.readouterr().out
        assert "draft for you to approve" in out
        assert "2 draft fact(s) came with it" in out
        assert json.loads(recorder.seen[-1].content)["answer"] == "2023-06 to 2023-08"

    def test_questions_are_listed_with_their_lines(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        recorder = Recorder(
            {
                ("GET", "/questions"): {
                    "open_count": 1,
                    "questions": [
                        {
                            "question_id": QUESTION_ID,
                            "kind": "missing_dates",
                            "prompt": "When did Software Engineering Intern start and end?",
                            "detail": "'Summer 2023' names a season rather than a month.",
                            "status": "open",
                            "source_lines": [2],
                            "created_at": "2026-10-08T00:00:00Z",
                        }
                    ],
                }
            }
        )
        assert run(recorder, ["questions"], monkeypatch) == 0
        out = capsys.readouterr().out
        # The id has to be readable: it is the thing you cannot retype from memory.
        assert QUESTION_ID in out
        assert "(line 2)" in out
        assert "aria answer" in out

    def test_facts_prints_the_next_command(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        recorder = Recorder(
            {
                ("GET", "/facts"): {
                    "total": 1,
                    "facts": [
                        {
                            "fact_id": FACT_ID,
                            "text": "Built 20+ React components used by 4 teams",
                            "version": 1,
                            "status": "draft",
                            "source": "resume_import",
                            "source_line": 7,
                        }
                    ],
                }
            }
        )
        assert run(recorder, ["facts"], monkeypatch) == 0
        out = capsys.readouterr().out
        assert FACT_ID in out
        assert "line 7" in out
        assert "aria approve <id>" in out

    def test_import_mentions_the_waiting_questions(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        recorder = Recorder(
            {
                ("POST", f"/uploads/{UPLOAD_ID}/import"): {
                    "upload_id": UPLOAD_ID,
                    "counts": {"facts": 3},
                    "summary": ["3 draft fact(s)"],
                    "questions_queued": 2,
                    "held_back": [],
                }
            }
        )
        assert run(recorder, ["import", UPLOAD_ID], monkeypatch) == 0
        out = capsys.readouterr().out
        assert "3 draft fact(s)" in out
        assert "2 question(s) waiting" in out

    def test_a_refused_import_says_nothing_was_written_and_why(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        recorder = Recorder(
            {
                ("POST", f"/uploads/{UPLOAD_ID}/import"): {
                    "upload_id": UPLOAD_ID,
                    "status": "needs_user_fix",
                    "counts": {},
                    "summary": [],
                    "reasons": [
                        "6 of 20 entries do not have the shape of an entry, too many to trust the rest"
                    ],
                    "dropped": ["experience at line 12 (25%): the employer holds a metric"],
                    "flags": [],
                }
            }
        )
        assert run(recorder, ["import", UPLOAD_ID], monkeypatch) == 1
        out = capsys.readouterr().out
        assert "NOT IMPORTED: nothing was written" in out
        assert "6 of 20 entries" in out
        assert "line 12 (25%)" in out
        assert f"aria lines {UPLOAD_ID}" in out

    def test_an_import_shows_what_it_dropped_and_what_to_check(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        recorder = Recorder(
            {
                ("POST", f"/uploads/{UPLOAD_ID}/import"): {
                    "upload_id": UPLOAD_ID,
                    "status": "imported",
                    "counts": {"facts": 3},
                    "summary": ["3 draft fact(s)"],
                    "dropped": ["bullet at line 9 under Nordvale: shorter than 20 characters"],
                    "flags": ["Nordvale: the start date is more than a year ahead"],
                }
            }
        )
        assert run(recorder, ["import", UPLOAD_ID], monkeypatch) == 0
        out = capsys.readouterr().out
        assert "bullet at line 9" in out
        assert "CHECK      Nordvale: the start date" in out

    def test_lines_prints_each_line_with_its_kind_and_dates(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        recorder = Recorder(
            {
                ("GET", f"/uploads/{UPLOAD_ID}/lines"): {
                    "upload_id": UPLOAD_ID,
                    "lines": [
                        {"number": 1, "page": 1, "column": 0, "kind": "name", "text": "JORDAN A. RIVERA"},
                        {
                            "number": 12,
                            "page": 1,
                            "column": 0,
                            "kind": "entry_header",
                            "text": "Nordvale Consulting Feb 2024 - Jul 2025",
                            "date_text": "Feb 2024 - Jul 2025",
                        },
                    ],
                    "ambiguous_pages": [2],
                }
            }
        )
        assert run(recorder, ["lines", UPLOAD_ID], monkeypatch) == 0
        out = capsys.readouterr().out.splitlines()
        assert out[0].split() == ["1", "p1", "c0", "name", "JORDAN", "A.", "RIVERA"]
        assert "entry_header" in out[1] and out[1].endswith("[Feb 2024 - Jul 2025]")
        assert "not settled on page 2" in out[-1]

    def test_upload_asks_for_a_fresh_reading_only_when_told(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        resume = tmp_path / "resume.pdf"
        resume.write_bytes(b"%PDF-1.4 not really")
        answer = {"upload_id": UPLOAD_ID, "detected_type": "pdf", "detail": ""}
        for argv, expected in (
            (["upload", str(resume)], "false"),
            (["upload", str(resume), "--reprocess"], "true"),
        ):
            recorder = Recorder({("POST", "/uploads"): answer})
            assert run(recorder, argv, monkeypatch) == 0
            assert recorder.seen[0].url.params["reprocess"] == expected

    def test_a_fresh_reading_says_what_it_replaced(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        resume = tmp_path / "resume.pdf"
        resume.write_bytes(b"%PDF-1.4 not really")
        earlier = str(uuid.uuid4())
        recorder = Recorder(
            {
                ("POST", "/uploads"): {
                    "upload_id": UPLOAD_ID,
                    "detected_type": "pdf",
                    "detail": "extractor=pdfplumber",
                    "reprocessed_from": earlier,
                }
            }
        )
        assert run(recorder, ["upload", str(resume)], monkeypatch) == 0
        out = capsys.readouterr().out
        assert f"replaces upload {earlier}" in out


class TestReview:
    def test_review_posts_and_says_when(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        recorder = Recorder(
            {
                ("POST", f"/uploads/{UPLOAD_ID}/review"): {
                    "upload_id": UPLOAD_ID,
                    "reviewed_at": "2026-10-15T16:40:00+00:00",
                    "reviewed_by": str(uuid.uuid4()),
                }
            }
        )
        assert run(recorder, ["review", UPLOAD_ID], monkeypatch) == 0
        assert f"upload {UPLOAD_ID} reviewed at 2026-10-15" in capsys.readouterr().out


class TestReconsiderAQuestion:
    def test_it_goes_to_the_question_route_and_says_what_happened(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        recorder = Recorder(
            {
                ("POST", f"/questions/{QUESTION_ID}/reconsider"): {
                    "question_id": QUESTION_ID,
                    "outcome": "withdrawn",
                    "detail": "import the upload again and it will be asked afresh",
                }
            }
        )
        assert run(recorder, ["reconsider", QUESTION_ID, "--kind", "question"], monkeypatch) == 0
        assert "withdrawn: import the upload again" in capsys.readouterr().out
        assert [request.url.path for request in recorder.seen] == [f"/questions/{QUESTION_ID}/reconsider"]


class TestWithdraw:
    NEW = str(uuid.uuid4())
    PREVIEW: ClassVar[dict[str, Any]] = {
        "upload_id": UPLOAD_ID,
        "superseded_by": NEW,
        "withdrawn": False,
        "counts": {"facts": 23, "roles": 9, "education": 0, "projects": 0, "questions": 18},
        "kept": {"approved facts": 1},
    }

    def _recorder(self) -> Recorder:
        done = {**self.PREVIEW, "withdrawn": True}
        return Recorder(
            {
                ("GET", f"/uploads/{UPLOAD_ID}/withdrawal"): self.PREVIEW,
                ("POST", f"/uploads/{UPLOAD_ID}/withdraw"): done,
            }
        )

    def test_the_count_comes_first_and_no_means_nothing_happens(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        recorder = self._recorder()
        monkeypatch.setattr("builtins.input", lambda _prompt: "n")
        assert run(recorder, ["withdraw", UPLOAD_ID], monkeypatch) == 1
        out = capsys.readouterr().out
        assert "would delete   23  facts" in out and "would delete   18  questions" in out
        assert "keeps           1  approved facts" in out
        assert "nothing withdrawn" in out
        assert [request.method for request in recorder.seen] == ["GET"]

    def test_yes_at_the_prompt_withdraws(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        recorder = self._recorder()
        monkeypatch.setattr("builtins.input", lambda _prompt: "y")
        assert run(recorder, ["withdraw", UPLOAD_ID], monkeypatch) == 0
        out = capsys.readouterr().out
        assert "deleted        23  facts" in out
        assert f"next: aria import {self.NEW}" in out
        assert [request.method for request in recorder.seen] == ["GET", "POST"]

    def test_no_terminal_to_ask_is_a_no(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = self._recorder()

        def closed(_prompt: str) -> str:
            raise EOFError

        monkeypatch.setattr("builtins.input", closed)
        assert run(recorder, ["withdraw", UPLOAD_ID], monkeypatch) == 1
        assert [request.method for request in recorder.seen] == ["GET"]

    def test_yes_flag_skips_the_question(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = self._recorder()

        def never(_prompt: str) -> str:
            raise AssertionError("asked despite --yes")

        monkeypatch.setattr("builtins.input", never)
        assert run(recorder, ["withdraw", UPLOAD_ID, "--yes"], monkeypatch) == 0
        assert [request.method for request in recorder.seen] == ["GET", "POST"]

    def test_nothing_to_withdraw_asks_nothing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        empty = {**self.PREVIEW, "counts": dict.fromkeys(self.PREVIEW["counts"], 0), "kept": {}}
        recorder = Recorder({("GET", f"/uploads/{UPLOAD_ID}/withdrawal"): empty})
        assert run(recorder, ["withdraw", UPLOAD_ID], monkeypatch) == 0
        assert "nothing left to withdraw" in capsys.readouterr().out


class TestTheReportIsUsableInAScript:
    def test_a_complete_graph_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        recorder = Recorder(
            {
                ("GET", "/report/completeness"): {
                    "complete": True,
                    "checks": [],
                    "counts": {},
                    "summary": ["graph complete"],
                }
            }
        )
        assert run(recorder, ["report"], monkeypatch) == 0
        assert "graph complete" in capsys.readouterr().out

    def test_an_incomplete_graph_exits_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = Recorder(
            {
                ("GET", "/report/completeness"): {
                    "complete": False,
                    "checks": [],
                    "counts": {},
                    "summary": ["graph not complete yet", "  need skills: 3 approved"],
                }
            }
        )
        assert run(recorder, ["report"], monkeypatch) == 1


class TestErrorsAreReadable:
    def test_a_refusal_prints_the_api_sentence(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The API's refusals are written for a person to read. A traceback over the
        # top of one throws away the only useful part.
        recorder = Recorder(
            {
                ("POST", f"/questions/{QUESTION_ID}/answer"): httpx.Response(
                    422,
                    json={"detail": "'summer' is not a month I can read. Expected YYYY-MM."},
                )
            }
        )
        assert run(recorder, ["answer", QUESTION_ID, "summer"], monkeypatch) == 1
        assert "not a month I can read" in capsys.readouterr().err

    def test_a_validation_error_is_flattened(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        recorder = Recorder(
            {
                ("POST", f"/facts/{FACT_ID}/edit"): httpx.Response(
                    422,
                    json={
                        "detail": [
                            {"loc": ["body", "text"], "msg": "String should have at least 1 character"}
                        ]
                    },
                )
            }
        )
        assert run(recorder, ["edit", FACT_ID, "x"], monkeypatch) == 1
        assert "body.text" in capsys.readouterr().err

    def test_an_unreachable_api_says_so(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("nope", request=request)

        client = AriaClient("http://testserver", TOKEN)
        client._client = httpx.Client(transport=httpx.MockTransport(refuse), base_url="http://testserver")
        with pytest.raises(ApiError, match="Is it running"):
            client.health()

    def test_no_token_is_a_usage_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["facts"]) == 2
        assert "no API token" in capsys.readouterr().err

    def test_a_token_in_the_working_directory_is_never_used_by_a_test(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Regression: the test above once read the developer's real .env, found their
        # token and sent GET /facts to their running API. Here a .env with a token sits
        # in the working directory, pointing at a port nothing listens on, so without
        # the conftest guard this fails on any machine — and contacts nobody.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]
        (tmp_path / ".env").write_text(
            "ARIA_DATABASE_URL=postgresql+asyncpg://nobody:x@127.0.0.1:1/none\n"
            "ARIA_API_SERVICE_TOKEN=a-token-no-test-may-use\n"
            f"ARIA_API_BIND_PORT={free_port}\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        assert main(["facts"]) == 2
        assert "no API token" in capsys.readouterr().err

    def test_uploading_a_missing_file_is_a_usage_error(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        recorder = Recorder({})
        assert run(recorder, ["upload", "does-not-exist.pdf"], monkeypatch) == 2
        assert "no such file" in capsys.readouterr().err
        # Nothing was sent: the file is checked before the request.
        assert recorder.seen == []


class TestTheInterviewVerbs:
    def test_gaps_is_read_only_and_points_at_the_interview(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        recorder = Recorder(
            {
                ("GET", "/interview/gaps"): {
                    "total": 2,
                    "gaps": [
                        {
                            "kind": "missing_context",
                            "prompt": "What did you actually build or change at Zelis?",
                            "detail": "This role is on your graph with no achievements under it.",
                            "priority": 0,
                        },
                        {
                            "kind": "unevidenced_skill",
                            "prompt": "You list Kafka as a skill, but none of your facts mention it.",
                            "detail": "A listed skill with nothing behind it is a keyword.",
                            "priority": 3,
                        },
                    ],
                }
            }
        )
        assert run(recorder, ["gaps"], monkeypatch) == 0
        out = capsys.readouterr().out
        assert "2 gap(s)" in out
        assert "missing_context" in out
        assert "aria interview enrichment" in out
        # Read-only: one GET and nothing else.
        assert [request.method for request in recorder.seen] == ["GET"]

    def test_enrichment_passes_the_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = Recorder(
            {
                ("POST", "/interview/enrichment"): {
                    "interview": "enrichment",
                    "queued": 2,
                    "prompts": ["What did you build at Zelis?", "A number for the claims export?"],
                    "remaining_gaps": 7,
                    "summary": "2 question(s) queued; 7 gap(s) known in total.",
                }
            }
        )
        assert run(recorder, ["interview", "enrichment", "--limit", "2"], monkeypatch) == 0
        assert recorder.seen[-1].url.params["limit"] == "2"

    def test_enrichment_prints_what_it_queued(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        recorder = Recorder(
            {
                ("POST", "/interview/enrichment"): {
                    "interview": "enrichment",
                    "queued": 1,
                    "prompts": ["What did you actually build or change at Zelis?"],
                    "remaining_gaps": 4,
                    "summary": "1 question(s) queued; 4 gap(s) known in total.",
                }
            }
        )
        assert run(recorder, ["interview", "enrichment"], monkeypatch) == 0
        out = capsys.readouterr().out
        assert "1 question(s) queued" in out
        assert "Zelis" in out
        assert "aria answer" in out

    def test_motivations_takes_no_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The six are fixed, so there is nothing to cap.
        recorder = Recorder(
            {
                ("POST", "/interview/motivations"): {
                    "interview": "motivations",
                    "queued": 6,
                    "prompts": [],
                    "remaining_gaps": 6,
                    "summary": "6 of 6 motivation question(s) queued; 0 already answered",
                }
            }
        )
        assert run(recorder, ["interview", "motivations"], monkeypatch) == 0
        assert recorder.seen[-1].url.path == "/interview/motivations"
        assert "limit" not in recorder.seen[-1].url.params

    def test_an_unknown_interview_is_a_usage_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            main(["--token", TOKEN, "interview", "vibes"])
        assert "invalid choice" in capsys.readouterr().err


class TestWhereTheTokenComesFrom:
    """One machine, one file. The environment wins; .env is the fallback."""

    def test_the_environment_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARIA_API_SERVICE_TOKEN", "from-the-environment")
        parser = build_parser()
        assert parser.parse_args(["health"]).token == "from-the-environment"

    def test_a_flag_beats_both(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARIA_API_SERVICE_TOKEN", "from-the-environment")
        parser = build_parser()
        assert parser.parse_args(["--token", "explicit", "health"]).token == "explicit"

    def test_a_broken_env_file_is_not_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The CLI does not need a database URL, so a missing or half-written .env
        # means "no default" rather than a crash on startup.
        monkeypatch.delenv("ARIA_API_SERVICE_TOKEN", raising=False)
        monkeypatch.setattr("aria_cli.main._from_env_file", lambda: ("", ""))
        parser = build_parser()
        assert parser.parse_args(["health"]).token == ""

    def test_the_url_falls_back_to_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ARIA_API_URL", raising=False)
        monkeypatch.setattr("aria_cli.main._from_env_file", lambda: ("", ""))
        parser = build_parser()
        assert parser.parse_args(["health"]).url == DEFAULT_URL
