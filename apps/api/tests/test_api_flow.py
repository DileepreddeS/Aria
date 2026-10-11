"""The whole approval flow over HTTP: upload, import, answer, approve, report.

This is the surface the CLI and the Phase 6 dashboard both go through, so it is the
place to prove the properties that must not depend on which client is asking:

* an unauthenticated request gets nothing, and an API with no token configured
  refuses to serve rather than allowing everything;
* ``approved_by`` is the authenticated caller and cannot be supplied by the client;
* the lifecycle is enforced here too — approving a rejected fact is a 409;
* a refused answer comes back with what was expected, because "invalid" is not
  something a person can act on;
* "graph complete" is a set of checks, and the report says which ones failed.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from pydantic import SecretStr
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_api.app import create_app
from aria_api.auth import API_TOKEN_HEADER
from aria_core.config import Settings
from aria_core.db.models import Candidate, Fact
from aria_core.db.session import tenant_transaction
from aria_core.schemas.identity import TenantId, UserId, new_id

pytestmark = pytest.mark.db

TOKEN = "test-token-not-a-secret"

RESUME = b"""Dileep Kumar Salla
Dallas, TX | (469) 555-0134 | dileep@example.test

EXPERIENCE
Accenture - Software Engineer
Jun 2024 - Present
- Built 20+ React components used by 4 teams
- Cut p95 API latency by 30% by adding Redis caching
- Drove $500K in savings by retiring a legacy service

Zelis - Software Engineering Intern
Summer 2023
- Automated a manual claims report with Python

EDUCATION
BS Computer Science, University of Texas at Dallas
Aug 2020 - May 2024

TECHNICAL SKILLS
Languages: Python, TypeScript, C++
Frameworks: React, FastAPI
Infrastructure: AWS, Docker, PostgreSQL, Redis
"""


@pytest.fixture
async def candidate_id(engine: AsyncEngine, tenant_a: TenantId, user_a: UserId) -> uuid.UUID:
    created = new_id()
    async with tenant_transaction(engine, tenant_a) as session:
        await session.execute(insert(Candidate).values(id=created, tenant_id=tenant_a, user_id=user_a))
    return created


@pytest.fixture
def api_settings(
    settings: Settings,
    tenant_a: TenantId,
    user_a: UserId,
    candidate_id: uuid.UUID,
    tmp_path_factory: pytest.TempPathFactory,
) -> Settings:
    """Settings with a known token and this test's identity."""
    return settings.model_copy(
        update={
            "api_service_token": SecretStr(TOKEN),
            "api_user_id": str(user_a),
            "api_tenant_id": str(tenant_a),
            "api_candidate_id": str(candidate_id),
            "upload_quarantine_path": tmp_path_factory.mktemp("quarantine"),
        }
    )


@pytest_asyncio.fixture
async def client(api_settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(api_settings)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={API_TOKEN_HEADER: TOKEN},
            timeout=60.0,
        ) as http:
            yield http


async def _upload_and_import(client: httpx.AsyncClient, content: bytes = RESUME) -> dict[str, Any]:
    response = await client.post("/uploads", files={"file": ("resume.txt", content, "text/plain")})
    assert response.status_code == 201, response.text
    upload = response.json()
    assert upload["refusal_reason"] is None, upload

    imported = await client.post(f"/uploads/{upload['upload_id']}/import")
    assert imported.status_code == 200, imported.text
    return {"upload": upload, "import": imported.json()}


class TestAuthentication:
    async def test_health_needs_no_token(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/health", headers={API_TOKEN_HEADER: ""})
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_no_token_is_rejected(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/facts", headers={API_TOKEN_HEADER: ""})
        assert response.status_code == 401

    async def test_a_wrong_token_is_rejected(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/facts", headers={API_TOKEN_HEADER: "not-the-token"})
        assert response.status_code == 401

    async def test_an_api_with_no_token_refuses_to_start(self, api_settings: Settings) -> None:
        # Not "allows everything". The API holds S1 data and can put claims on a
        # resume, so a missing token is a misconfiguration and it stops.
        app = create_app(api_settings.model_copy(update={"api_service_token": None}))
        with pytest.raises(RuntimeError, match="will not serve unauthenticated"):
            async with LifespanManager(app):
                pass  # pragma: no cover

    async def test_an_api_with_no_identity_cannot_serve(self, api_settings: Settings) -> None:
        app = create_app(api_settings.model_copy(update={"api_candidate_id": ""}))
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                headers={API_TOKEN_HEADER: TOKEN},
            ) as http:
                response = await http.get("/facts")
        assert response.status_code == 503
        assert "api_candidate_id" in response.json()["detail"]


class TestUploadAndImport:
    async def test_a_text_resume_round_trips(self, client: httpx.AsyncClient) -> None:
        result = await _upload_and_import(client)
        assert result["upload"]["detected_type"] == "txt"
        assert result["import"]["counts"]["facts"] >= 3

    async def test_the_import_reports_what_it_held_back(self, client: httpx.AsyncClient) -> None:
        # "Summer 2023" is a season, so the internship is held back with a question.
        result = await _upload_and_import(client)
        assert any("Intern" in line for line in result["import"]["held_back"])
        assert result["import"]["questions_queued"] >= 1

    async def test_an_unsupported_file_is_refused_with_advice(self, client: httpx.AsyncClient) -> None:
        # A 200 with a reason, not a 4xx: the uploads row and its audit events are
        # the product of ingestion even for a file that was turned away.
        response = await client.post(
            "/uploads", files={"file": ("photo.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64, "image/png")}
        )
        assert response.status_code == 201
        body = response.json()
        # The reason names what the file actually is, so the advice can be specific.
        assert body["refusal_reason"] in {"unsupported_type", "image_upload"}
        assert body["refusal_advice"]

    async def test_importing_an_unknown_upload_is_a_404(self, client: httpx.AsyncClient) -> None:
        response = await client.post(f"/uploads/{new_id()}/import")
        assert response.status_code == 404

    async def test_importing_a_refused_upload_is_a_409(self, client: httpx.AsyncClient) -> None:
        scan = await client.post(
            "/uploads", files={"file": ("scan.pdf", b"%PDF-1.4\n" + b"0" * 400, "application/pdf")}
        )
        body = scan.json()
        if body.get("refusal_reason") is None:  # pragma: no cover - fixture sanity
            pytest.skip("that PDF was accepted; this test needs a refused one")
        response = await client.post(f"/uploads/{body['upload_id']}/import")
        assert response.status_code in {404, 409}


class TestTheApprovalFlow:
    async def test_drafts_come_back_with_their_source(self, client: httpx.AsyncClient) -> None:
        await _upload_and_import(client)
        response = await client.get("/facts")
        assert response.status_code == 200
        facts = response.json()["facts"]
        assert facts
        for fact in facts:
            assert fact["status"] == "draft"
            # A list of ids is useless to somebody deciding what to approve.
            assert fact["text"]
            assert fact["source_upload_id"]
            assert fact["source_line"]

    async def test_approving_makes_it_approved(self, client: httpx.AsyncClient) -> None:
        await _upload_and_import(client)
        fact = (await client.get("/facts")).json()["facts"][0]
        response = await client.post(f"/facts/{fact['fact_id']}/approve", json={})
        assert response.status_code == 200
        assert response.json()["status"] == "approved"

        approved = (await client.get("/facts", params={"status": "approved"})).json()
        assert fact["fact_id"] in {item["fact_id"] for item in approved["facts"]}

    async def test_the_approver_is_the_caller_whatever_the_body_says(
        self,
        client: httpx.AsyncClient,
        engine: AsyncEngine,
        tenant_a: TenantId,
        user_a: UserId,
    ) -> None:
        # A client that could name its own approver would make the lifecycle
        # decorative. The route takes no body at all, which is stronger than
        # validating one: there is nowhere to put a different approver.
        await _upload_and_import(client)
        fact = (await client.get("/facts")).json()["facts"][0]
        imposter = new_id()
        response = await client.post(f"/facts/{fact['fact_id']}/approve", json={"approved_by": str(imposter)})
        assert response.status_code == 200

        async with tenant_transaction(engine, tenant_a) as session:
            approved_by = (
                await session.execute(select(Fact.approved_by).where(Fact.id == uuid.UUID(fact["fact_id"])))
            ).scalar_one()
        assert approved_by == user_a
        assert approved_by != imposter

    async def test_rejecting_keeps_the_fact(self, client: httpx.AsyncClient) -> None:
        await _upload_and_import(client)
        fact = (await client.get("/facts")).json()["facts"][0]
        response = await client.post(f"/facts/{fact['fact_id']}/reject", json={"reason": "not mine"})
        assert response.status_code == 200
        assert response.json()["status"] == "rejected"
        rejected = (await client.get("/facts", params={"status": "rejected"})).json()
        assert fact["fact_id"] in {item["fact_id"] for item in rejected["facts"]}

    async def test_editing_appends_a_revision(self, client: httpx.AsyncClient) -> None:
        await _upload_and_import(client)
        fact = (await client.get("/facts")).json()["facts"][0]
        response = await client.post(
            f"/facts/{fact['fact_id']}/edit",
            json={"text": "Built 22 React components used by 4 teams"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["version"] == fact["version"] + 1
        assert body["text"] == "Built 22 React components used by 4 teams"
        assert body["status"] == "approved"

    async def test_an_edit_may_stay_a_draft(self, client: httpx.AsyncClient) -> None:
        await _upload_and_import(client)
        fact = (await client.get("/facts")).json()["facts"][0]
        response = await client.post(
            f"/facts/{fact['fact_id']}/edit",
            json={"text": "Built some React components", "approve": False},
        )
        assert response.json()["status"] == "draft"

    async def test_the_lifecycle_is_enforced(self, client: httpx.AsyncClient) -> None:
        # Approving a rejected fact is not a client-side rule.
        await _upload_and_import(client)
        fact = (await client.get("/facts")).json()["facts"][0]
        await client.post(f"/facts/{fact['fact_id']}/reject", json={})
        response = await client.post(f"/facts/{fact['fact_id']}/approve", json={})
        assert response.status_code == 409
        assert "rejected" in response.json()["detail"]

    async def test_a_rejected_fact_can_be_reconsidered(self, client: httpx.AsyncClient) -> None:
        await _upload_and_import(client)
        fact = (await client.get("/facts")).json()["facts"][0]
        await client.post(f"/facts/{fact['fact_id']}/reject", json={})
        response = await client.post(f"/facts/{fact['fact_id']}/reconsider", json={})
        assert response.status_code == 200
        assert response.json()["status"] == "draft"

    async def test_an_unknown_fact_is_a_404(self, client: httpx.AsyncClient) -> None:
        response = await client.post(f"/facts/{new_id()}/approve", json={})
        assert response.status_code == 404


class TestQuestionsOverHttp:
    async def test_the_queue_is_listed(self, client: httpx.AsyncClient) -> None:
        await _upload_and_import(client)
        response = await client.get("/questions")
        assert response.status_code == 200
        body = response.json()
        assert body["open_count"] >= 1
        kinds = {question["kind"] for question in body["questions"]}
        assert "missing_dates" in kinds

    async def test_answering_creates_the_held_back_role_as_a_draft(self, client: httpx.AsyncClient) -> None:
        await _upload_and_import(client)
        question = next(
            item
            for item in (await client.get("/questions")).json()["questions"]
            if item["kind"] == "missing_dates"
        )
        response = await client.post(
            f"/questions/{question['question_id']}/answer",
            json={"answer": "2023-06 to 2023-08"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["subject_kind"] == "experience"
        assert body["fact_ids"]
        assert "draft" in body["summary"]

    async def test_a_refused_answer_says_what_was_expected(self, client: httpx.AsyncClient) -> None:
        # "Invalid answer" is not something a person can act on.
        await _upload_and_import(client)
        question = next(
            item
            for item in (await client.get("/questions")).json()["questions"]
            if item["kind"] == "missing_dates"
        )
        response = await client.post(
            f"/questions/{question['question_id']}/answer", json={"answer": "summer"}
        )
        assert response.status_code == 422
        assert "2023-06" in response.json()["detail"]

    async def test_a_question_can_be_dismissed(self, client: httpx.AsyncClient) -> None:
        await _upload_and_import(client)
        question = (await client.get("/questions")).json()["questions"][0]
        response = await client.post(
            f"/questions/{question['question_id']}/dismiss", json={"reason": "not relevant"}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "dismissed"

    async def test_answering_twice_is_a_conflict(self, client: httpx.AsyncClient) -> None:
        await _upload_and_import(client)
        question = next(
            item
            for item in (await client.get("/questions")).json()["questions"]
            if item["kind"] == "missing_dates"
        )
        await client.post(
            f"/questions/{question['question_id']}/answer", json={"answer": "2023-06 to 2023-08"}
        )
        again = await client.post(
            f"/questions/{question['question_id']}/answer", json={"answer": "2024-01 to 2024-02"}
        )
        assert again.status_code == 422
        assert "already answered" in again.json()["detail"]


class TestTheCompletenessReport:
    async def test_an_empty_graph_is_not_complete(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/report/completeness")
        assert response.status_code == 200
        body = response.json()
        assert body["complete"] is False
        assert {check["name"] for check in body["checks"]} >= {
            "approved_experience",
            "approved_facts",
            "contact_details",
            "education",
            "skills",
            "no_open_questions",
        }

    async def test_every_failing_check_says_what_is_missing(self, client: httpx.AsyncClient) -> None:
        # A gate nobody can read is not a gate.
        body = (await client.get("/report/completeness")).json()
        for check in body["checks"]:
            if not check["met"]:
                assert check["detail"]

    async def test_importing_does_not_make_it_complete(self, client: httpx.AsyncClient) -> None:
        # Everything imported is a draft. Completeness counts approved rows, so an
        # import on its own moves nothing — which is the point.
        await _upload_and_import(client)
        body = (await client.get("/report/completeness")).json()
        assert body["complete"] is False
        assert body["counts"]["approved_facts"] == 0
        assert body["counts"]["open_questions"] >= 1

    async def test_open_questions_alone_keep_it_incomplete(self, client: httpx.AsyncClient) -> None:
        await _upload_and_import(client)
        for fact in (await client.get("/facts")).json()["facts"]:
            await client.post(f"/facts/{fact['fact_id']}/approve", json={})
        body = (await client.get("/report/completeness")).json()
        unmet = {check["name"] for check in body["checks"] if not check["met"]}
        assert "no_open_questions" in unmet


class TestUploadingTheSameFileTwice:
    """Clicking upload again is a thing people do, not an error.

    The uploads table has a unique index on (candidate_id, sha256) so a byte-identical
    re-upload can be recognised. Before the route used it, the second attempt hit the
    constraint and the integrity error reached the client as a 500.
    """

    async def test_the_second_upload_returns_the_first(self, client: httpx.AsyncClient) -> None:
        first = await client.post("/uploads", files={"file": ("resume.txt", RESUME, "text/plain")})
        assert first.status_code == 201
        second = await client.post("/uploads", files={"file": ("resume.txt", RESUME, "text/plain")})
        assert second.status_code == 201, second.text

        body = second.json()
        assert body["duplicate_of"] == first.json()["upload_id"]
        assert body["upload_id"] == first.json()["upload_id"]
        assert "already uploaded" in body["detail"]

    async def test_nothing_is_reprocessed(self, client: httpx.AsyncClient) -> None:
        # Parsing somebody's PDF in a subprocess is the expensive part, and the
        # result is already on the row.
        await client.post("/uploads", files={"file": ("resume.txt", RESUME, "text/plain")})
        second = await client.post("/uploads", files={"file": ("resume.txt", RESUME, "text/plain")})
        assert second.json()["parse_status"] == "parsed"
        assert second.json()["characters"] > 0

    async def test_a_different_file_is_still_a_new_upload(self, client: httpx.AsyncClient) -> None:
        first = await client.post("/uploads", files={"file": ("a.txt", RESUME, "text/plain")})
        other = await client.post(
            "/uploads", files={"file": ("b.txt", RESUME + b"\nAWARDS\nDean's List\n", "text/plain")}
        )
        assert other.json()["duplicate_of"] is None
        assert other.json()["upload_id"] != first.json()["upload_id"]

    async def test_re_importing_does_not_double_the_graph(self, client: httpx.AsyncClient) -> None:
        # The whole loop twice: same upload id, same facts, no new questions.
        first = await _upload_and_import(client)
        again = await client.post("/uploads", files={"file": ("resume.txt", RESUME, "text/plain")})
        reimport = await client.post(f"/uploads/{again.json()['upload_id']}/import")
        assert reimport.status_code == 200

        body = reimport.json()
        assert body["counts"]["facts"] == 0
        assert body["counts"]["duplicates_skipped"] == first["import"]["counts"]["facts"]
        assert body["questions_queued"] == 0


class TestTheInterviewsOverHttp:
    """Starting an interview only queues questions; answering goes through /questions.

    One code path from an answer to the graph, which is why there is no
    ``/interview/.../answer``.
    """

    async def test_gaps_are_read_only(self, client: httpx.AsyncClient) -> None:
        # Seeing what is missing must not fill the queue.
        await _upload_and_import(client)
        before = (await client.get("/questions")).json()["open_count"]
        response = await client.get("/interview/gaps")
        assert response.status_code == 200
        assert response.json()["total"] > 0
        assert (await client.get("/questions")).json()["open_count"] == before

    async def test_every_gap_says_why_it_is_asked(self, client: httpx.AsyncClient) -> None:
        await _upload_and_import(client)
        for gap in (await client.get("/interview/gaps")).json()["gaps"]:
            assert gap["prompt"]
            assert gap["detail"]

    async def test_starting_enrichment_queues_questions(self, client: httpx.AsyncClient) -> None:
        await _upload_and_import(client)
        response = await client.post("/interview/enrichment")
        assert response.status_code == 201
        body = response.json()
        assert body["queued"] > 0
        assert len(body["prompts"]) == body["queued"]

        queue = (await client.get("/questions")).json()
        assert queue["open_count"] >= body["queued"]

    async def test_the_round_is_capped(self, client: httpx.AsyncClient) -> None:
        await _upload_and_import(client)
        body = (await client.post("/interview/enrichment", params={"limit": 2})).json()
        assert body["queued"] == 2
        # And it says how much is still known to be missing, so the cap is not a lie
        # about the state of the graph.
        assert body["remaining_gaps"] > 2

    async def test_answering_an_enrichment_question_creates_a_draft_fact(
        self, client: httpx.AsyncClient
    ) -> None:
        await _upload_and_import(client)
        await client.post("/interview/enrichment")
        question = next(
            item
            for item in (await client.get("/questions")).json()["questions"]
            if item["kind"] in {"missing_metric", "missing_context", "unevidenced_skill"}
        )
        response = await client.post(
            f"/questions/{question['question_id']}/answer",
            json={"answer": "It cut the nightly run from 40 minutes to 6 minutes"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["subject_kind"] == "fact"

        drafts = (await client.get("/facts")).json()["facts"]
        created = next(fact for fact in drafts if fact["fact_id"] == body["subject_id"])
        assert created["status"] == "draft"
        assert created["source"] == "enrichment_interview"

    async def test_the_motivations_interview_asks_six(self, client: httpx.AsyncClient) -> None:
        response = await client.post("/interview/motivations")
        assert response.status_code == 201
        assert response.json()["queued"] == 6

    async def test_a_motivation_answer_is_stored_verbatim(self, client: httpx.AsyncClient) -> None:
        await client.post("/interview/motivations")
        question = next(
            item
            for item in (await client.get("/questions")).json()["questions"]
            if item["kind"] == "motivation"
        )
        answer = "Making slow things fast, and untangling systems nobody wants to touch"
        response = await client.post(f"/questions/{question['question_id']}/answer", json={"answer": answer})
        assert response.status_code == 200
        assert response.json()["subject_kind"] == "motivation"

    async def test_re_running_the_motivations_interview_asks_nothing_new(
        self, client: httpx.AsyncClient
    ) -> None:
        await client.post("/interview/motivations")
        again = await client.post("/interview/motivations")
        assert again.json()["queued"] == 0

    async def test_an_answered_question_can_be_approved_into_the_report(
        self, client: httpx.AsyncClient
    ) -> None:
        # The loop that matters, end to end: the interview asks about a gap, the
        # answer arrives as a draft fact, and only *approving* it moves the
        # completeness report. An answer on its own moves nothing.
        await _upload_and_import(client)
        before = (await client.get("/report/completeness")).json()["counts"]["approved_facts"]

        await client.post("/interview/enrichment", params={"limit": 50})
        question = next(
            item
            for item in (await client.get("/questions")).json()["questions"]
            if item["kind"] in {"missing_metric", "missing_context", "unevidenced_skill"}
        )
        answered = await client.post(
            f"/questions/{question['question_id']}/answer",
            json={"answer": "I used it for the nightly reconciliation job at Accenture"},
        )
        fact_id = answered.json()["subject_id"]

        # Answered, not approved: the report has not moved.
        assert (await client.get("/report/completeness")).json()["counts"]["approved_facts"] == before

        assert (await client.post(f"/facts/{fact_id}/approve", json={})).status_code == 200
        assert (await client.get("/report/completeness")).json()["counts"]["approved_facts"] == before + 1

    async def test_a_contact_detail_is_only_asked_for_when_it_is_missing(
        self, client: httpx.AsyncClient
    ) -> None:
        # This resume's header supplies all five, so the interview does not ask about
        # any of them. Asking for something already on the graph is the noise that
        # makes a queue unreadable.
        await _upload_and_import(client)
        await client.post("/interview/enrichment", params={"limit": 50})
        kinds = {item["kind"] for item in (await client.get("/questions")).json()["questions"]}
        assert "missing_profile_field" not in kinds


class TestTheGateCanActuallyBePassed:
    """The completeness report must be satisfiable, or it teaches you to ignore it.

    Before /entities existed it was not: the report counts approved roles, education,
    skills and contact details, and nothing could approve any of them. This walks the
    whole thing to ``complete: true``.
    """

    async def test_entities_are_listed_with_a_summary(self, client: httpx.AsyncClient) -> None:
        await _upload_and_import(client)
        response = await client.get("/entities/experience")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] >= 1
        for row in body["entities"]:
            assert row["status"] == "draft"
            # A list of ids is not something anybody can approve.
            assert row["summary"]

    async def test_approving_a_role_records_the_caller(
        self, client: httpx.AsyncClient, user_a: UserId
    ) -> None:
        await _upload_and_import(client)
        role = (await client.get("/entities/experience")).json()["entities"][0]
        response = await client.post(f"/entities/experience/{role['entity_id']}/approve", json={})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "approved"
        assert body["approved_by"] == str(user_a)

    async def test_the_lifecycle_is_enforced_here_too(self, client: httpx.AsyncClient) -> None:
        await _upload_and_import(client)
        role = (await client.get("/entities/experience")).json()["entities"][0]
        await client.post(f"/entities/experience/{role['entity_id']}/reject", json={})
        again = await client.post(f"/entities/experience/{role['entity_id']}/approve", json={})
        assert again.status_code == 409

    async def test_an_unknown_kind_is_a_validation_error(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/entities/vibes")
        assert response.status_code == 422

    async def test_the_graph_can_reach_complete(self, client: httpx.AsyncClient) -> None:
        # Approve everything the report asks for, answer every question, and check
        # that it actually says complete. This is the Phase 1 gate.
        await _upload_and_import(client)
        await client.post("/interview/enrichment", params={"limit": 50})
        await client.post("/interview/motivations")

        # Answer or dismiss every open question. The report refuses to call a graph
        # complete while ARIA is still waiting on an answer.
        for question in (await client.get("/questions")).json()["questions"]:
            answered = await client.post(
                f"/questions/{question['question_id']}/answer",
                json={"answer": "I built the nightly reconciliation job in Python"},
            )
            if answered.status_code != 200:
                await client.post(
                    f"/questions/{question['question_id']}/dismiss",
                    json={"reason": "not relevant"},
                )

        # Approve every draft fact, then every draft row of every kind.
        for fact in (await client.get("/facts")).json()["facts"]:
            await client.post(f"/facts/{fact['fact_id']}/approve", json={})
        for kind in (
            "experience",
            "education",
            "project",
            "profile_field",
            "declared_skill",
            "achievement",
            "motivation",
        ):
            for row in (await client.get(f"/entities/{kind}")).json()["entities"]:
                await client.post(f"/entities/{kind}/{row['entity_id']}/approve", json={})

        body = (await client.get("/report/completeness")).json()
        unmet = [check["name"] + ": " + check["detail"] for check in body["checks"] if not check["met"]]
        assert body["complete"] is True, f"still unmet: {unmet}"
