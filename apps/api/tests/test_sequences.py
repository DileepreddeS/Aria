"""Sequences, not operations: every path through upload, import, withdraw and reprocess
ends in the graph a single clean import makes.

Every bug since Stage 0 was a sequence bug, each found by walking a path by hand:
uploading twice handed back a stale reading; importing twice doubled projects;
withdrawing then importing lost bullets; answering then withdrawing lost the answer
and blocked the question. Each operation was right on its own. This file runs the
paths, each for a fresh candidate, and compares the graph each leaves with the graph
a clean import and the same answer leave.

The comparison is a fingerprint of the graph without ids or upload ids: roles,
degrees, projects, facts under their parents, profile fields, declared skills,
achievements, open questions and answered ones, each with its status.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from pydantic import SecretStr
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_api.app import create_app
from aria_api.auth import API_TOKEN_HEADER
from aria_core.config import Settings
from aria_core.db.models import (
    Achievement,
    Candidate,
    DeclaredSkill,
    Education,
    Experience,
    Fact,
    FactRevision,
    ProfileField,
    Project,
    User,
    UserQuestion,
)
from aria_core.db.session import tenant_transaction
from aria_core.schemas.identity import TenantId, UserId, new_id

pytestmark = pytest.mark.db

TOKEN = "test-token-not-a-secret"
FIXTURE = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "resumes" / "clean_inline_block.pdf"


class Person:
    """One candidate's view of the API, and the ids a sequence carries between steps."""

    def __init__(self, client: httpx.AsyncClient, candidate_id: uuid.UUID) -> None:
        self.client = client
        self.candidate_id = candidate_id
        self.uploads: list[str] = []

    async def upload(self, *, reprocess: bool = False) -> None:
        response = await self.client.post(
            "/uploads",
            files={"file": (FIXTURE.name, FIXTURE.read_bytes(), "application/pdf")},
            params={"reprocess": reprocess},
        )
        assert response.status_code == 201, response.text
        upload_id = response.json()["upload_id"]
        if upload_id not in self.uploads:
            self.uploads.append(upload_id)

    async def import_(self, which: int = -1) -> None:
        response = await self.client.post(f"/uploads/{self.uploads[which]}/import")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "imported", response.json()["reasons"]

    async def withdraw_replaced(self) -> None:
        response = await self.client.post(f"/uploads/{self.uploads[-2]}/withdraw")
        assert response.status_code == 200, response.text

    async def answer_paid(self) -> None:
        """Answer every open "were you paid?" with yes, as the candidate did."""
        questions = (await self.client.get("/questions")).json()["questions"]
        for question in questions:
            if question["kind"] == "paid_or_unpaid":
                answered = await self.client.post(
                    f"/questions/{question['question_id']}/answer", json={"answer": "yes"}
                )
                assert answered.status_code == 200, answered.text


Step = Callable[[Person], Awaitable[None]]


def upload(c: Person) -> Awaitable[None]:
    return c.upload()


def reprocess(c: Person) -> Awaitable[None]:
    return c.upload(reprocess=True)


def import_(c: Person) -> Awaitable[None]:
    return c.import_()


def withdraw(c: Person) -> Awaitable[None]:
    return c.withdraw_replaced()


def answer(c: Person) -> Awaitable[None]:
    return c.answer_paid()


CLEAN: list[Step] = [upload, import_, answer]
SEQUENCES: dict[str, list[Step]] = {
    "import twice": [upload, import_, import_, answer],
    "answer, then import again": [upload, import_, answer, import_],
    "upload twice": [upload, upload, import_, answer],
    "reprocess, withdraw, import": [upload, import_, reprocess, withdraw, import_, answer],
    "answer, reprocess, withdraw, import, answer": [
        upload,
        import_,
        answer,
        reprocess,
        withdraw,
        import_,
        answer,
    ],
    "reprocess, import new first, withdraw, import again": [
        upload,
        import_,
        answer,
        reprocess,
        import_,
        withdraw,
        import_,
        answer,
    ],
    "reprocess twice, withdraw each": [
        upload,
        import_,
        answer,
        reprocess,
        withdraw,
        import_,
        reprocess,
        withdraw,
        import_,
        answer,
    ],
}


async def fingerprint(engine: AsyncEngine, tenant: TenantId, candidate_id: uuid.UUID) -> dict[str, Any]:
    """The graph as the candidate would read it: no ids, no upload ids."""
    async with tenant_transaction(engine, tenant) as session:

        async def rows(*columns: Any, model: Any) -> list[tuple[Any, ...]]:
            result = await session.execute(select(*columns).where(model.candidate_id == candidate_id))
            return sorted((tuple(row) for row in result.all()), key=repr)

        roles = {
            row[0]: (row[1], row[2])
            for row in (
                await session.execute(
                    select(Experience.id, Experience.employer, Experience.title).where(
                        Experience.candidate_id == candidate_id
                    )
                )
            ).all()
        }
        projects = {
            row[0]: row[1]
            for row in (
                await session.execute(
                    select(Project.id, Project.name).where(Project.candidate_id == candidate_id)
                )
            ).all()
        }
        facts = sorted(
            (
                (
                    row[0],
                    row[1],
                    roles.get(row[2]) if row[2] else None,
                    projects.get(row[3]) if row[3] else None,
                )
                for row in (
                    await session.execute(
                        select(FactRevision.text, Fact.status, Fact.experience_id, Fact.project_id)
                        .join(
                            FactRevision,
                            (FactRevision.fact_id == Fact.id)
                            & (FactRevision.version == Fact.current_version),
                        )
                        .where(Fact.candidate_id == candidate_id)
                    )
                ).all()
            ),
            key=repr,
        )
        return {
            "roles": await rows(
                Experience.employer,
                Experience.title,
                Experience.kind,
                Experience.start_month,
                Experience.end_month,
                Experience.status,
                model=Experience,
            ),
            "degrees": await rows(
                Education.school, Education.degree, Education.end_month, Education.status, model=Education
            ),
            "projects": await rows(Project.name, Project.status, model=Project),
            "facts": facts,
            "profile": await rows(
                ProfileField.kind, ProfileField.value, ProfileField.status, model=ProfileField
            ),
            "skills": await rows(DeclaredSkill.skill_canonical, DeclaredSkill.status, model=DeclaredSkill),
            "achievements": await rows(Achievement.title, Achievement.status, model=Achievement),
            "questions": await rows(
                UserQuestion.kind,
                UserQuestion.prompt,
                UserQuestion.status,
                UserQuestion.answer,
                model=UserQuestion,
            ),
        }


@pytest.fixture
def base_settings(
    settings: Settings, tenant_a: TenantId, tmp_path_factory: pytest.TempPathFactory
) -> Settings:
    return settings.model_copy(
        update={
            "api_service_token": SecretStr(TOKEN),
            "api_tenant_id": str(tenant_a),
            "upload_quarantine_path": tmp_path_factory.mktemp("quarantine"),
            "parser_isolation": "process",
        }
    )


@asynccontextmanager
async def candidate(engine: AsyncEngine, tenant: TenantId, settings: Settings) -> AsyncIterator[Person]:
    """A fresh user and candidate in the tenant, and a client acting as them."""
    user_id, candidate_id = UserId(new_id()), new_id()
    async with tenant_transaction(engine, tenant) as session:
        await session.execute(
            insert(User).values(
                id=user_id,
                tenant_id=tenant,
                email=f"user-{user_id.hex[:8]}@example.test",
                display_name="Test Person",
            )
        )
        await session.execute(insert(Candidate).values(id=candidate_id, tenant_id=tenant, user_id=user_id))
    app = create_app(
        settings.model_copy(update={"api_user_id": str(user_id), "api_candidate_id": str(candidate_id)})
    )
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={API_TOKEN_HEADER: TOKEN},
            timeout=60.0,
        ) as client:
            yield Person(client, candidate_id)


async def _run(
    engine: AsyncEngine, tenant: TenantId, settings: Settings, steps: list[Step]
) -> dict[str, Any]:
    async with candidate(engine, tenant, settings) as person:
        for step in steps:
            await step(person)
        return await fingerprint(engine, tenant, person.candidate_id)


class TestEveryPathEndsInTheCleanGraph:
    async def test_the_clean_path_answers_the_assistantship_as_paid(
        self, engine: AsyncEngine, tenant_a: TenantId, base_settings: Settings
    ) -> None:
        # The baseline is worth something only if it holds the answer the sequences lose.
        clean = await _run(engine, tenant_a, base_settings, CLEAN)
        assert ("Cascadia State University", "Graduate Research Assistant") in [
            row[:2] for row in clean["roles"]
        ]
        assert [row[2] for row in clean["roles"] if row[1] == "Graduate Research Assistant"] == ["part_time"]
        assert len(clean["projects"]) == 3 and len(clean["degrees"]) == 2

    @pytest.mark.parametrize("name", list(SEQUENCES))
    async def test_the_sequence_leaves_the_graph_a_clean_import_leaves(
        self, engine: AsyncEngine, tenant_a: TenantId, base_settings: Settings, name: str
    ) -> None:
        clean = await _run(engine, tenant_a, base_settings, CLEAN)
        walked = await _run(engine, tenant_a, base_settings, SEQUENCES[name])
        for part in clean:
            assert walked[part] == clean[part], f"{name}: {part} differs"
