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

import io
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pypdf
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from pydantic import SecretStr
from sqlalchemy import insert, null, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_api.app import create_app
from aria_api.auth import API_TOKEN_HEADER
from aria_core.audit.log import read_chain
from aria_core.config import Settings
from aria_core.db.models import (
    Candidate,
    Education,
    Experience,
    Fact,
    ProfileField,
    Project,
    Upload,
    UserQuestion,
)
from aria_core.db.session import tenant_transaction
from aria_core.schemas.identity import TenantId, UserId, new_id

pytestmark = pytest.mark.db

TOKEN = "test-token-not-a-secret"

RESUME = b"""Jordan A. Rivera
Portland, OR | (555) 014-2288 | jordan.rivera@example.com

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
BS Computer Science, Cascadia State University
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
            # These tests are about the API, not the container; the container has its
            # own tests in services/document_parser/tests/test_isolation.py.
            "parser_isolation": "process",
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

    The uploads table has a unique index on (candidate_id, sha256) over current
    readings, so a byte-identical re-upload can be recognised. Before the route used
    it, the second attempt hit the constraint and the integrity error reached the
    client as a 500.
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


# ----------------------------------------------------------------- Stage 0 gates

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "resumes"
GATE = [
    "clean_inline_block",
    "float_scrambled",
    "markerless_bullets",
    "no_marker_bullets",
    "per_glyph_contact",
]


def _truth(name: str) -> dict[str, Any]:
    truth: dict[str, Any] = json.loads((FIXTURES / f"{name}.truth.json").read_text(encoding="utf-8"))
    return truth


def _month(value: Any) -> str | None:
    return None if value is None else value.isoformat()[:7]


def _flat_text(name: str) -> bytes:
    reader = pypdf.PdfReader(io.BytesIO((FIXTURES / f"{name}.pdf").read_bytes()))
    return "\n".join(page.extract_text() for page in reader.pages).encode("utf-8")


def _ambiguous_pdf() -> bytes:
    """Two columns with a banner across the gutter mid-page: an order nothing settles."""
    left = ["Achievement line number one for the left column"] * 9
    right = ["Skills sidebar entry"] * 9
    parts = [b"BT /F1 11 Tf 1 0 0 1 50 640 Tm (PROFESSIONAL DEVELOPMENT, AWARDS, CERTIFICATIONS AND 2024) Tj"]
    for index in range(9):
        for x, column in ((50, left), (330, right)):
            parts.append(f" 1 0 0 1 {x} {720 - index * 18} Tm ({column[index]}) Tj".encode())
    parts.append(b" ET")
    stream = b"".join(parts)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


async def _upload(client: httpx.AsyncClient, name: str, content: bytes, media: str) -> dict[str, Any]:
    response = await client.post("/uploads", files={"file": (name, content, media)})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    assert body["refusal_reason"] is None, body
    return body


async def _graph_rows(engine: AsyncEngine, tenant: TenantId) -> int:
    async with tenant_transaction(engine, tenant) as session:
        counts = [
            len((await session.execute(select(model.id))).all())
            for model in (Experience, Education, Project, Fact, ProfileField)
        ]
    return sum(counts)


class TestTheFixtureGate:
    """RESUME_ENGINE §5.9 end to end: upload each gate fixture, import it, read the graph."""

    @pytest.mark.parametrize("name", GATE)
    async def test_the_graph_holds_exactly_what_the_truth_file_does(
        self, client: httpx.AsyncClient, engine: AsyncEngine, tenant_a: TenantId, name: str
    ) -> None:
        upload = await _upload(
            client, f"{name}.pdf", (FIXTURES / f"{name}.pdf").read_bytes(), "application/pdf"
        )
        imported = (await client.post(f"/uploads/{upload['upload_id']}/import")).json()
        assert imported["status"] == "imported", imported["reasons"]
        assert imported["dropped"] == [] and imported["flags"] == []

        truth = [entry for entry in _truth(name)["entries"]]
        async with tenant_transaction(engine, tenant_a) as session:
            roles = (
                await session.execute(
                    select(
                        Experience.id,
                        Experience.employer,
                        Experience.title,
                        Experience.start_month,
                        Experience.end_month,
                    ).order_by(Experience.source_line)
                )
            ).all()
            degrees = (
                await session.execute(
                    select(Education.school, Education.degree).order_by(Education.source_line)
                )
            ).all()
            projects = (
                await session.execute(select(Project.id, Project.name).order_by(Project.source_line))
            ).all()
            facts = (await session.execute(select(Fact.experience_id, Fact.project_id))).all()

        want_roles = [entry for entry in truth if entry["section"] == "experience"]
        assert [(row[1], row[2], _month(row[3]), _month(row[4]) or "present") for row in roles] == [
            (entry["employer"], entry["title"], entry["start"], entry["end"]) for entry in want_roles
        ]
        for row, entry in zip(roles, want_roles, strict=True):
            assert sum(1 for fact in facts if fact[0] == row[0]) == entry["bullet_count"], row[1]
        assert [tuple(row) for row in degrees] == [
            (entry["employer"], entry["title"]) for entry in truth if entry["section"] == "education"
        ]
        want_projects = [entry for entry in truth if entry["section"] == "project"]
        assert [row[1] for row in projects] == [entry["name"] for entry in want_projects]
        for project, entry in zip(projects, want_projects, strict=True):
            # The header's description is a fact of its own beside the bullets.
            assert sum(1 for fact in facts if fact[1] == project[0]) == entry["bullet_count"] + 1, project[1]


class TestTheImportGates:
    async def test_a_scrambled_reading_is_refused_and_nothing_is_written(
        self, client: httpx.AsyncClient, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        # The old importer's door: float_scrambled's flat text, pasted as a text file.
        upload = await _upload(client, "resume.txt", _flat_text("float_scrambled"), "text/plain")
        response = await client.post(f"/uploads/{upload['upload_id']}/import")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "needs_user_fix"
        assert any("do not have the shape of an entry" in reason for reason in body["reasons"])
        assert body["counts"] == {} and body["summary"] == []
        assert await _graph_rows(engine, tenant_a) == 0

    async def test_the_refusal_is_audited_by_gate_without_resume_text(
        self, client: httpx.AsyncClient, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        upload = await _upload(client, "resume.txt", _flat_text("float_scrambled"), "text/plain")
        body = (await client.post(f"/uploads/{upload['upload_id']}/import")).json()
        async with tenant_transaction(engine, tenant_a) as session:
            events = await read_chain(session, tenant_a)
        (event,) = [event for event in events if event.action == "graph.import_refused"]
        assert event.outcome == "denied"
        assert event.payload["gates"] == "shape_failures"
        assert event.payload["entries_dropped"] == len(
            [line for line in body["dropped"] if not line.startswith("bullet")]
        )
        recorded = json.dumps(event.payload) + event.reason
        for words in ("Nordvale", "Brightfold", "Pinecrest", "Cascadia", "jordan.rivera"):
            assert words not in recorded
        # Nor any dropped entry's own words, whatever they are: "(label): reason".
        labels = [line.split(" (", 1)[1].rsplit("): ", 1)[0] for line in body["dropped"] if " (" in line]
        labels = [label for label in labels if label]  # an entry with no employer has no words
        assert labels
        for label in labels:
            assert label not in recorded
        assert not [event for event in events if event.action == "graph.imported"]

    async def test_columns_the_geometry_cannot_order_are_parsed_then_refused_at_import(
        self, client: httpx.AsyncClient, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        upload = await _upload(client, "resume.pdf", _ambiguous_pdf(), "application/pdf")
        assert upload["parse_status"] == "parsed" and upload["needs_review"] is True
        body = (await client.post(f"/uploads/{upload['upload_id']}/import")).json()
        assert body["status"] == "needs_user_fix"
        assert body["reasons"][0].startswith("page 1 is laid out in columns")
        assert await _graph_rows(engine, tenant_a) == 0

    async def test_a_text_resume_that_has_the_shape_of_one_still_imports(
        self, client: httpx.AsyncClient
    ) -> None:
        body = (await _upload_and_import(client))["import"]
        assert body["status"] == "imported" and body["reasons"] == []


class TestTheLines:
    async def test_every_line_comes_back_classified_and_numbered_like_a_source_line(
        self, client: httpx.AsyncClient
    ) -> None:
        name = "no_marker_bullets"
        upload = await _upload(
            client, f"{name}.pdf", (FIXTURES / f"{name}.pdf").read_bytes(), "application/pdf"
        )
        response = await client.get(f"/uploads/{upload['upload_id']}/lines")
        assert response.status_code == 200
        lines = response.json()["lines"]
        assert [line["number"] for line in lines] == list(range(1, len(lines) + 1))
        kinds = [line["kind"] for line in lines]
        truth = _truth(name)["entries"]
        assert kinds.count("entry_header") == len(truth)
        assert kinds.count("bullet") == sum(entry["bullet_count"] for entry in truth)
        nordvale = next(line for line in lines if line["text"].startswith("Nordvale Consulting"))
        assert nordvale["kind"] == "entry_header" and nordvale["date_text"].startswith("Feb 2024")
        assert response.json()["ambiguous_pages"] == []

    async def test_an_unsettled_page_is_named(self, client: httpx.AsyncClient) -> None:
        upload = await _upload(client, "resume.pdf", _ambiguous_pdf(), "application/pdf")
        body = (await client.get(f"/uploads/{upload['upload_id']}/lines")).json()
        assert body["ambiguous_pages"] == [1]

    async def test_a_text_upload_has_no_lines_to_show(self, client: httpx.AsyncClient) -> None:
        upload = await _upload(client, "resume.txt", RESUME, "text/plain")
        response = await client.get(f"/uploads/{upload['upload_id']}/lines")
        assert response.status_code == 409
        assert "PDF" in response.json()["detail"]

    async def test_an_unknown_upload_is_a_404(self, client: httpx.AsyncClient) -> None:
        assert (await client.get(f"/uploads/{new_id()}/lines")).status_code == 404


class TestAFileReadBeforeStageZero:
    """An upload the old parser read: a PDF, parsed, no layout, "agreement 100%".

    Re-uploading the same bytes used to hand that record back as a duplicate. It is
    read again instead, into a new upload that supersedes the old one, which is kept
    unchanged because drafts imported from it quote its lines.
    """

    NAME = "clean_inline_block"

    async def _stale_upload(
        self, client: httpx.AsyncClient, engine: AsyncEngine, tenant: TenantId, *, json_null: bool = False
    ) -> str:
        """Upload the PDF, then make its row look exactly like one from before S0-12."""
        content = (FIXTURES / f"{self.NAME}.pdf").read_bytes()
        upload = await _upload(client, f"{self.NAME}.pdf", content, "application/pdf")
        async with tenant_transaction(engine, tenant) as session:
            await session.execute(
                update(Upload)
                .where(Upload.id == uuid.UUID(upload["upload_id"]))
                .values(
                    # SQL NULL, as on a row written before migration 0005 added the column;
                    # or JSON null, which is how the pipeline writes "no layout" since.
                    layout=None if json_null else null(),
                    cross_extractor=None,
                    parse_detail="extractor=pypdf; 1 page(s); agreement 100%",
                    extracted_text="JORDAN A. RIVERA\nflat text in an order nobody checked",
                )
            )
        return str(upload["upload_id"])

    async def _again(self, client: httpx.AsyncClient, *, reprocess: bool = False) -> dict[str, Any]:
        content = (FIXTURES / f"{self.NAME}.pdf").read_bytes()
        response = await client.post(
            "/uploads",
            files={"file": (f"{self.NAME}.pdf", content, "application/pdf")},
            params={"reprocess": reprocess},
        )
        assert response.status_code == 201, response.text
        body: dict[str, Any] = response.json()
        return body

    async def test_uploading_it_again_reads_it_again(
        self, client: httpx.AsyncClient, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        old = await self._stale_upload(client, engine, tenant_a)
        body = await self._again(client)
        assert body["upload_id"] != old
        assert body["reprocessed_from"] == old and body["duplicate_of"] is None
        assert "agreement" not in body["detail"] and "cross-extractor: agree" in body["detail"]
        lines = await client.get(f"/uploads/{body['upload_id']}/lines")
        assert lines.status_code == 200 and len(lines.json()["lines"]) > 0

    async def test_a_missing_layout_stored_as_json_null_is_missing_too(
        self, client: httpx.AsyncClient, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        old = await self._stale_upload(client, engine, tenant_a, json_null=True)
        assert (await self._again(client))["reprocessed_from"] == old

    async def test_the_old_row_is_kept_as_it_was_and_points_at_the_new_one(
        self, client: httpx.AsyncClient, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        old = await self._stale_upload(client, engine, tenant_a)
        body = await self._again(client)
        async with tenant_transaction(engine, tenant_a) as session:
            row = (
                await session.execute(
                    select(Upload.superseded_by, Upload.extracted_text, Upload.parse_detail).where(
                        Upload.id == uuid.UUID(old)
                    )
                )
            ).one()
            events = await read_chain(session, tenant_a)
        assert str(row[0]) == body["upload_id"]
        # Drafts imported from it quote these lines, so they are not rewritten.
        assert row[1] == "JORDAN A. RIVERA\nflat text in an order nobody checked"
        received = [event for event in events if event.action == "upload.received"]
        assert received[-1].payload["supersedes"] == old

    async def test_the_third_upload_is_a_duplicate_of_the_second(
        self, client: httpx.AsyncClient, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        await self._stale_upload(client, engine, tenant_a)
        second = await self._again(client)
        third = await self._again(client)
        assert third["duplicate_of"] == second["upload_id"] and third["reprocessed_from"] is None

    async def test_it_is_not_imported_from_its_flat_text(
        self, client: httpx.AsyncClient, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        old = await self._stale_upload(client, engine, tenant_a)
        response = await client.post(f"/uploads/{old}/import")
        assert response.status_code == 409
        assert "upload the file again" in response.json()["detail"]
        assert await _graph_rows(engine, tenant_a) == 0

    async def test_its_lines_say_how_to_get_them(
        self, client: httpx.AsyncClient, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        old = await self._stale_upload(client, engine, tenant_a)
        response = await client.get(f"/uploads/{old}/lines")
        assert response.status_code == 409
        assert "upload the file again" in response.json()["detail"]

    async def test_once_superseded_the_old_upload_names_its_replacement(
        self, client: httpx.AsyncClient, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        old = await self._stale_upload(client, engine, tenant_a)
        body = await self._again(client)
        response = await client.post(f"/uploads/{old}/import")
        assert response.status_code == 409
        assert body["upload_id"] in response.json()["detail"]

    async def test_reprocess_reads_a_current_upload_again_on_request(self, client: httpx.AsyncClient) -> None:
        first = await _upload(
            client, f"{self.NAME}.pdf", (FIXTURES / f"{self.NAME}.pdf").read_bytes(), "application/pdf"
        )
        assert (await self._again(client))["duplicate_of"] == first["upload_id"]
        forced = await self._again(client, reprocess=True)
        assert forced["reprocessed_from"] == first["upload_id"] and forced["upload_id"] != first["upload_id"]

    async def test_a_text_upload_has_no_layout_and_is_still_a_duplicate(
        self, client: httpx.AsyncClient
    ) -> None:
        first = await client.post("/uploads", files={"file": ("resume.txt", RESUME, "text/plain")})
        second = await client.post("/uploads", files={"file": ("resume.txt", RESUME, "text/plain")})
        assert second.json()["duplicate_of"] == first.json()["upload_id"]
        assert second.json()["reprocessed_from"] is None


class TestWithdrawingAReplacedReading:
    """`aria withdraw`: one step for the drafts and questions a superseded upload left.

    The old reading is a text resume imported and then read again with --reprocess:
    real drafts, real roles, real open questions, on an upload that is no longer current.
    """

    async def _replaced(self, client: httpx.AsyncClient) -> tuple[str, str, dict[str, Any]]:
        old = await _upload_and_import(client)
        again = await client.post(
            "/uploads", files={"file": ("resume.txt", RESUME, "text/plain")}, params={"reprocess": True}
        )
        new = again.json()
        assert new["reprocessed_from"] == old["upload"]["upload_id"]
        return old["upload"]["upload_id"], new["upload_id"], old["import"]

    @staticmethod
    async def _left(engine: AsyncEngine, tenant: TenantId, upload_id: str) -> dict[str, int]:
        source = uuid.UUID(upload_id)
        async with tenant_transaction(engine, tenant) as session:
            return {
                model.__tablename__: len(
                    (await session.execute(select(model.id).where(model.source_upload_id == source))).all()
                )
                for model in (Fact, Experience, Education, Project, ProfileField, UserQuestion)
            }

    async def test_the_preview_counts_what_the_import_wrote_and_changes_nothing(
        self, client: httpx.AsyncClient, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        old, new, imported = await self._replaced(client)
        before = await self._left(engine, tenant_a, old)
        response = await client.get(f"/uploads/{old}/withdrawal")
        assert response.status_code == 200
        body = response.json()
        assert body["withdrawn"] is False and body["superseded_by"] == new
        assert body["counts"]["facts"] == imported["counts"]["facts"] == before["facts"]
        assert body["counts"]["roles"] == before["experiences"] > 0
        assert body["counts"]["questions"] == before["user_questions"] > 0
        assert await self._left(engine, tenant_a, old) == before

    async def test_withdrawing_deletes_every_draft_and_open_question_from_it(
        self, client: httpx.AsyncClient, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        old, _, _ = await self._replaced(client)
        preview = (await client.get(f"/uploads/{old}/withdrawal")).json()
        body = (await client.post(f"/uploads/{old}/withdraw")).json()
        assert body["withdrawn"] is True and body["counts"] == preview["counts"]
        assert set((await self._left(engine, tenant_a, old)).values()) == {0}

    async def test_an_approved_fact_survives_and_so_does_its_role(
        self, client: httpx.AsyncClient, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        old, _, _ = await self._replaced(client)
        fact = (await client.get("/facts")).json()["facts"][0]
        assert (await client.post(f"/facts/{fact['fact_id']}/approve", json={})).status_code == 200
        body = (await client.post(f"/uploads/{old}/withdraw")).json()
        assert body["kept"]["approved facts"] == 1
        assert body["kept"]["roles still holding facts that stay"] == 1
        async with tenant_transaction(engine, tenant_a) as session:
            survivors = (
                await session.execute(
                    select(Fact.id, Fact.experience_id).where(Fact.source_upload_id == uuid.UUID(old))
                )
            ).all()
            role = (
                await session.execute(select(Experience.id).where(Experience.id == survivors[0][1]))
            ).all()
        assert [str(row[0]) for row in survivors] == [fact["fact_id"]]
        assert len(role) == 1

    async def test_the_audit_event_says_what_went_and_what_replaced_it(
        self, client: httpx.AsyncClient, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        old, new, _ = await self._replaced(client)
        body = (await client.post(f"/uploads/{old}/withdraw")).json()
        async with tenant_transaction(engine, tenant_a) as session:
            events = await read_chain(session, tenant_a)
        (event,) = [event for event in events if event.action == "graph.withdrawn"]
        assert event.payload["upload_id"] == old and event.payload["superseded_by"] == new
        for name in ("facts", "roles", "education", "projects", "questions"):
            assert event.payload[name] == body["counts"][name]
        assert "Accenture" not in json.dumps(event.payload) + event.reason

    async def test_the_new_reading_imports_in_full_afterwards(self, client: httpx.AsyncClient) -> None:
        # Deleted, not rejected or dismissed: nothing withdrawn blocks the same bullet
        # or the same question from the new reading.
        old, new, imported = await self._replaced(client)
        await client.post(f"/uploads/{old}/withdraw")
        body = (await client.post(f"/uploads/{new}/import")).json()
        assert body["counts"]["facts"] == imported["counts"]["facts"]
        assert body["counts"].get("rejected_again", 0) == 0
        assert body["questions_queued"] == imported["questions_queued"]

    async def test_importing_the_new_reading_first_then_again_loses_nothing(
        self, client: httpx.AsyncClient
    ) -> None:
        # The other order: the new reading's identical bullets are skipped as duplicates
        # of the old drafts, which the withdrawal then deletes. Importing again restores them.
        old, new, imported = await self._replaced(client)
        first = (await client.post(f"/uploads/{new}/import")).json()
        assert first["counts"]["facts"] == 0
        await client.post(f"/uploads/{old}/withdraw")
        again = (await client.post(f"/uploads/{new}/import")).json()
        assert again["counts"]["facts"] == imported["counts"]["facts"]

    async def test_a_current_upload_cannot_be_withdrawn_wholesale(self, client: httpx.AsyncClient) -> None:
        upload = (await _upload_and_import(client))["upload"]
        for method, path in (("GET", "withdrawal"), ("POST", "withdraw")):
            response = await client.request(method, f"/uploads/{upload['upload_id']}/{path}")
            assert response.status_code == 409
            assert "current reading" in response.json()["detail"]

    async def test_an_unknown_upload_is_a_404(self, client: httpx.AsyncClient) -> None:
        assert (await client.get(f"/uploads/{new_id()}/withdrawal")).status_code == 404
        assert (await client.post(f"/uploads/{new_id()}/withdraw")).status_code == 404

    async def test_withdrawing_twice_finds_nothing_the_second_time(self, client: httpx.AsyncClient) -> None:
        old, _, _ = await self._replaced(client)
        await client.post(f"/uploads/{old}/withdraw")
        second = (await client.post(f"/uploads/{old}/withdraw")).json()
        assert set(second["counts"].values()) == {0}


class TestImportingTheSameUploadTwice:
    """A second import of the same reading changes nothing: every kind of row is reused.

    Roles were always matched on employer and months; degrees, projects and awards were
    inserted again, so a re-import doubled them while each bullet, a duplicate of its
    own text, was skipped and left under the first copy.
    """

    @staticmethod
    async def _rows(engine: AsyncEngine, tenant: TenantId) -> dict[str, int]:
        async with tenant_transaction(engine, tenant) as session:
            return {
                model.__tablename__: len((await session.execute(select(model.id))).all())
                for model in (Experience, Education, Project, Fact, ProfileField)
            }

    async def test_the_graph_is_the_same_after_the_second_import(
        self, client: httpx.AsyncClient, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        name = "clean_inline_block"
        upload = await _upload(
            client, f"{name}.pdf", (FIXTURES / f"{name}.pdf").read_bytes(), "application/pdf"
        )
        first = (await client.post(f"/uploads/{upload['upload_id']}/import")).json()
        after_first = await self._rows(engine, tenant_a)
        second = (await client.post(f"/uploads/{upload['upload_id']}/import")).json()

        assert await self._rows(engine, tenant_a) == after_first
        assert (after_first["experiences"], after_first["education"], after_first["projects"]) == (4, 2, 3)
        assert second["counts"]["experiences_created"] == 0
        assert second["counts"]["educations"] == 0 and second["counts"]["educations_reused"] == 2
        assert second["counts"]["projects"] == 0 and second["counts"]["projects_reused"] == 3
        assert second["counts"]["facts"] == 0
        assert second["counts"]["duplicates_skipped"] == first["counts"]["facts"]


class TestReconsideringAQuestionOverHttp:
    async def test_a_dismissal_is_taken_back_and_the_question_is_open_again(
        self, client: httpx.AsyncClient
    ) -> None:
        await _upload_and_import(client)
        question = (await client.get("/questions")).json()["questions"][0]
        await client.post(f"/questions/{question['question_id']}/dismiss", json={"reason": "later"})
        response = await client.post(f"/questions/{question['question_id']}/reconsider")
        assert response.status_code == 200
        assert response.json()["outcome"] == "reopened"
        reopened = [row["question_id"] for row in (await client.get("/questions")).json()["questions"]]
        assert question["question_id"] in reopened

    async def test_an_open_question_is_a_409(self, client: httpx.AsyncClient) -> None:
        await _upload_and_import(client)
        question = (await client.get("/questions")).json()["questions"][0]
        response = await client.post(f"/questions/{question['question_id']}/reconsider")
        assert response.status_code == 409
        assert "still open" in response.json()["detail"]

    async def test_an_unknown_question_is_a_404(self, client: httpx.AsyncClient) -> None:
        assert (await client.post(f"/questions/{new_id()}/reconsider")).status_code == 404
