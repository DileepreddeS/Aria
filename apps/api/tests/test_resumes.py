"""Step 11b over HTTP: build a resume for a posting, then read back its PDF, reasons and gaps.

The graph is the flow test's text resume, imported and approved whole. The page is printed
through Chromium in the facts' own words (no model) and matched with the word-hash
embedder, as CI runs it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from pydantic import SecretStr
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_api.app import create_app
from aria_api.auth import API_TOKEN_HEADER
from aria_core.audit.log import read_chain
from aria_core.config import Settings
from aria_core.db.models import Candidate, Resume
from aria_core.db.session import tenant_transaction
from aria_core.schemas.identity import TenantId, UserId, new_id

pytestmark = [pytest.mark.db, pytest.mark.chromium]

TOKEN = "test-token-not-a-secret"
JD = (
    Path(__file__).resolve().parents[3]
    / "engines"
    / "resume"
    / "tests"
    / "fixtures"
    / "jds"
    / "larkspur_backend.txt"
).read_text(encoding="utf-8")
RESUME = b"""Jordan A. Rivera
Portland, OR | (555) 014-2288 | jordan.rivera@example.com

EXPERIENCE
Accenture - Software Engineer
Jun 2024 - Present
- Built 20+ React components used by 4 teams
- Cut p95 API latency by 30% by adding Redis caching

EDUCATION
BS Computer Science, Cascadia State University
Aug 2020 - May 2024

TECHNICAL SKILLS
Languages: Python, TypeScript
Infrastructure: AWS, Docker, PostgreSQL, Redis
"""
KINDS = ("experience", "project", "education", "profile_field", "declared_skill", "achievement")


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
    return settings.model_copy(
        update={
            "api_service_token": SecretStr(TOKEN),
            "api_user_id": str(user_a),
            "api_tenant_id": str(tenant_a),
            "api_candidate_id": str(candidate_id),
            "upload_quarantine_path": tmp_path_factory.mktemp("quarantine"),
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
            timeout=180.0,
        ) as http:
            yield http


async def _approved_graph(client: httpx.AsyncClient) -> None:
    upload = await client.post("/uploads", files={"file": ("resume.txt", RESUME, "text/plain")})
    assert upload.status_code == 201, upload.text
    imported = await client.post(f"/uploads/{upload.json()['upload_id']}/import")
    assert imported.status_code == 200, imported.text
    for kind in KINDS:
        for entity in (await client.get(f"/entities/{kind}")).json().get("entities", []):
            await client.post(f"/entities/{kind}/{entity['entity_id']}/approve", json={})
    for fact in (await client.get("/facts")).json()["facts"]:
        await client.post(f"/facts/{fact['fact_id']}/approve", json={})


async def _build(client: httpx.AsyncClient, **options: Any) -> dict[str, Any]:
    response = await client.post("/resumes", json={"jd_text": JD, "local_model": False, **options})
    assert response.status_code == 201, response.text
    built: dict[str, Any] = response.json()
    return built


class TestBuildingOne:
    async def test_a_page_is_built_stored_and_audited(
        self, client: httpx.AsyncClient, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        await _approved_graph(client)
        built = await _build(client)
        assert built["status"] in {"ok", "needs_review", "no_fit"}
        assert built["file_name"] == "Jordan_Rivera_Resume.pdf"
        assert built["used_model"] is False
        assert set(built["findings"]) == {"truth", "recruiter", "layout", "ats"}
        async with tenant_transaction(engine, tenant_a) as session:
            row = (
                await session.execute(select(Resume).where(Resume.id == uuid.UUID(built["id"])))
            ).scalar_one()
            assert row.pdf.startswith(b"%PDF") and row.jd_text == JD
            assert {"plan", "requirements", "bullets", "evidence_map", "validation_report", "facts"} <= set(
                row.artifacts
            )
            events = [
                event for event in await read_chain(session, tenant_a) if event.action == "resume.built"
            ]
        assert [event.subject_id for event in events] == [built["id"]]

    async def test_the_pdf_downloads_under_the_approved_name_only(self, client: httpx.AsyncClient) -> None:
        await _approved_graph(client)
        built = await _build(client)
        response = await client.get(f"/resumes/{built['id']}/pdf")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.headers["content-disposition"] == 'attachment; filename="Jordan_Rivera_Resume.pdf"'
        assert "Larkspur" not in response.headers["content-disposition"]
        assert response.content.startswith(b"%PDF")

    async def test_explain_gives_each_bullet_its_facts_in_their_words(
        self, client: httpx.AsyncClient
    ) -> None:
        await _approved_graph(client)
        built = await _build(client)
        bullets = (await client.get(f"/resumes/{built['id']}/explain")).json()["bullets"]
        assert bullets
        for bullet in bullets:
            assert bullet["facts"] and all(fact["text"] for fact in bullet["facts"])
        # In the facts' own words, each bullet is the text of the fact it cites.
        assert all(bullet["text"] in {fact["text"] for fact in bullet["facts"]} for bullet in bullets)

    async def test_detail_list_and_gaps(self, client: httpx.AsyncClient) -> None:
        await _approved_graph(client)
        first, second = await _build(client), await _build(client)
        listed = (await client.get("/resumes")).json()["resumes"]
        assert [item["id"] for item in listed] == [second["id"], first["id"]]
        detail = (await client.get(f"/resumes/{first['id']}")).json()
        assert detail["input_hash"] == (await client.get(f"/resumes/{second['id']}")).json()["input_hash"]
        assert "findings" in detail["validation_report"]
        gaps = (await client.get(f"/resumes/{first['id']}/gaps")).json()
        assert 0.0 <= gaps["coverage"] <= 1.0 and gaps["fit"] == first["fit"]


class TestRefusals:
    async def test_a_blank_posting_is_refused(self, client: httpx.AsyncClient) -> None:
        response = await client.post("/resumes", json={"jd_text": "", "local_model": False})
        assert response.status_code == 422

    async def test_no_approved_name_no_page(
        self, client: httpx.AsyncClient, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        response = await client.post("/resumes", json={"jd_text": JD, "local_model": False})
        assert response.status_code == 409
        async with tenant_transaction(engine, tenant_a) as session:
            assert (await session.execute(select(func.count()).select_from(Resume))).scalar_one() == 0

    async def test_another_candidate_s_resume_is_not_found(self, client: httpx.AsyncClient) -> None:
        response = await client.get(f"/resumes/{uuid.uuid4()}/pdf")
        assert response.status_code == 404

    async def test_a_written_page_is_never_checked_by_less_than_the_nli_model(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.post(
            "/resumes", json={"jd_text": JD, "use_model": True, "local_model": False}
        )
        assert response.status_code == 422
        assert "NLI" in response.json()["detail"]

    async def test_an_unknown_field_is_refused(self, client: httpx.AsyncClient) -> None:
        response = await client.post("/resumes", json={"jd_text": JD, "company_name_in_file": True})
        assert response.status_code == 422
