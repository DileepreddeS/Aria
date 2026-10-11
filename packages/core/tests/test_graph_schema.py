"""The graph schema enforces its own rules (RESUME_ENGINE §5).

Constraints are worth testing directly. A CHECK that was never exercised is a
comment: it looks like enforcement in review and does nothing in production.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_core.db.models import (
    GRAPH_TENANT_TABLES,
    Base,
    Candidate,
    Experience,
    Fact,
    FactRevision,
    ProfileField,
    Project,
)
from aria_core.db.session import tenant_transaction
from aria_core.graph.status import ApprovalStatus, ExperienceKind, GraphSource, ProfileFieldKind
from aria_core.schemas.identity import TenantId, UserId, new_id

pytestmark = pytest.mark.db


async def _candidate(engine: AsyncEngine, tenant_id: TenantId, user_id: UserId) -> uuid.UUID:
    candidate_id = new_id()
    async with tenant_transaction(engine, tenant_id) as session:
        await session.execute(insert(Candidate).values(id=candidate_id, tenant_id=tenant_id, user_id=user_id))
    return candidate_id


def _experience_values(
    tenant_id: TenantId, candidate_id: uuid.UUID, **overrides: object
) -> dict[str, object]:
    return {
        "id": new_id(),
        "tenant_id": tenant_id,
        "candidate_id": candidate_id,
        "employer": "Accenture",
        "title": "Software Engineer",
        "kind": ExperienceKind.PROFESSIONAL.value,
        "start_month": dt.date(2024, 6, 1),
        "end_month": None,
        "status": ApprovalStatus.DRAFT.value,
        "source": GraphSource.RESUME_IMPORT.value,
    } | overrides


class TestDatesAreMonths:
    async def test_a_month_is_accepted(self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId) -> None:
        candidate_id = await _candidate(engine, tenant_a, user_a)
        async with tenant_transaction(engine, tenant_a) as session:
            await session.execute(insert(Experience).values(**_experience_values(tenant_a, candidate_id)))

    async def test_a_mid_month_start_is_refused(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId
    ) -> None:
        # Resumes are month-precision. A day would be invented precision that then
        # has to be rendered away.
        candidate_id = await _candidate(engine, tenant_a, user_a)
        with pytest.raises((IntegrityError, DBAPIError), match="start_is_a_month"):
            async with tenant_transaction(engine, tenant_a) as session:
                await session.execute(
                    insert(Experience).values(
                        **_experience_values(tenant_a, candidate_id, start_month=dt.date(2024, 6, 17))
                    )
                )

    async def test_an_end_before_the_start_is_refused(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId
    ) -> None:
        candidate_id = await _candidate(engine, tenant_a, user_a)
        with pytest.raises((IntegrityError, DBAPIError), match="ends_after_it_starts"):
            async with tenant_transaction(engine, tenant_a) as session:
                await session.execute(
                    insert(Experience).values(
                        **_experience_values(
                            tenant_a,
                            candidate_id,
                            start_month=dt.date(2024, 6, 1),
                            end_month=dt.date(2024, 1, 1),
                        )
                    )
                )

    async def test_a_null_end_means_present(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId
    ) -> None:
        # Not "unknown": the importer writes NULL only for "Present", and
        # aria_core.graph.derive treats it as the current month.
        candidate_id = await _candidate(engine, tenant_a, user_a)
        async with tenant_transaction(engine, tenant_a) as session:
            await session.execute(
                insert(Experience).values(**_experience_values(tenant_a, candidate_id, end_month=None))
            )
            stored = await session.scalar(select(Experience.end_month))
        assert stored is None


class TestFactAnchoring:
    async def test_a_fact_may_have_one_anchor(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId
    ) -> None:
        candidate_id = await _candidate(engine, tenant_a, user_a)
        async with tenant_transaction(engine, tenant_a) as session:
            experience_id = new_id()
            await session.execute(
                insert(Experience).values(**_experience_values(tenant_a, candidate_id, id=experience_id))
            )
            await session.execute(
                insert(Fact).values(
                    id=new_id(),
                    tenant_id=tenant_a,
                    candidate_id=candidate_id,
                    experience_id=experience_id,
                    status=ApprovalStatus.DRAFT.value,
                    source=GraphSource.RESUME_IMPORT.value,
                )
            )

    async def test_a_fact_with_two_anchors_is_refused(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId
    ) -> None:
        # A fact on two experiences would appear twice on one resume.
        candidate_id = await _candidate(engine, tenant_a, user_a)
        with pytest.raises((IntegrityError, DBAPIError), match="at_most_one_anchor"):
            async with tenant_transaction(engine, tenant_a) as session:
                experience_id, project_id = new_id(), new_id()
                await session.execute(
                    insert(Experience).values(**_experience_values(tenant_a, candidate_id, id=experience_id))
                )
                await session.execute(
                    insert(Project).values(
                        id=project_id,
                        tenant_id=tenant_a,
                        candidate_id=candidate_id,
                        name="ARIA",
                        kind="personal",
                        status=ApprovalStatus.DRAFT.value,
                        source=GraphSource.RESUME_IMPORT.value,
                    )
                )
                await session.execute(
                    insert(Fact).values(
                        id=new_id(),
                        tenant_id=tenant_a,
                        candidate_id=candidate_id,
                        experience_id=experience_id,
                        project_id=project_id,
                        status=ApprovalStatus.DRAFT.value,
                        source=GraphSource.RESUME_IMPORT.value,
                    )
                )

    async def test_an_approved_fact_must_record_when(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId
    ) -> None:
        # "Approved" without a timestamp cannot be audited, so the schema refuses it.
        candidate_id = await _candidate(engine, tenant_a, user_a)
        with pytest.raises((IntegrityError, DBAPIError), match="approved_rows_record_when"):
            async with tenant_transaction(engine, tenant_a) as session:
                await session.execute(
                    insert(Fact).values(
                        id=new_id(),
                        tenant_id=tenant_a,
                        candidate_id=candidate_id,
                        status=ApprovalStatus.APPROVED.value,
                        source=GraphSource.RESUME_IMPORT.value,
                        approved_at=None,
                    )
                )


class TestFactTextIsImmutable:
    async def test_the_application_role_cannot_update_a_revision(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId
    ) -> None:
        candidate_id = await _candidate(engine, tenant_a, user_a)
        fact_id = new_id()
        async with tenant_transaction(engine, tenant_a) as session:
            await session.execute(
                insert(Fact).values(
                    id=fact_id,
                    tenant_id=tenant_a,
                    candidate_id=candidate_id,
                    status=ApprovalStatus.DRAFT.value,
                    source=GraphSource.RESUME_IMPORT.value,
                )
            )
            await session.execute(
                insert(FactRevision).values(
                    fact_id=fact_id,
                    version=1,
                    tenant_id=tenant_a,
                    text="Built 20+ React components used by 4 teams",
                    source=GraphSource.RESUME_IMPORT.value,
                )
            )

        # An edit appends a revision; it never rewrites one. That is what makes a
        # resume explainable months later (RESUME_ENGINE §1.10).
        with pytest.raises(DBAPIError, match=r"(?i)permission denied"):
            async with tenant_transaction(engine, tenant_a) as session:
                await session.execute(
                    update(FactRevision).where(FactRevision.fact_id == fact_id).values(text="reworded")
                )

    async def test_blank_text_is_refused(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId
    ) -> None:
        candidate_id = await _candidate(engine, tenant_a, user_a)
        fact_id = new_id()
        with pytest.raises((IntegrityError, DBAPIError), match="text_is_not_blank"):
            async with tenant_transaction(engine, tenant_a) as session:
                await session.execute(
                    insert(Fact).values(
                        id=fact_id,
                        tenant_id=tenant_a,
                        candidate_id=candidate_id,
                        status=ApprovalStatus.DRAFT.value,
                        source=GraphSource.RESUME_IMPORT.value,
                    )
                )
                await session.execute(
                    insert(FactRevision).values(
                        fact_id=fact_id,
                        version=1,
                        tenant_id=tenant_a,
                        text="   ",
                        source=GraphSource.RESUME_IMPORT.value,
                    )
                )


class TestProfileFields:
    async def test_only_one_approved_value_per_kind(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId
    ) -> None:
        # Two approved email addresses would make "what is your email?" ambiguous
        # at the moment a form is being filled in.
        candidate_id = await _candidate(engine, tenant_a, user_a)
        values = {
            "tenant_id": tenant_a,
            "candidate_id": candidate_id,
            "kind": ProfileFieldKind.EMAIL.value,
            "status": ApprovalStatus.APPROVED.value,
            "source": GraphSource.RESUME_IMPORT.value,
            "approved_at": dt.datetime.now(tz=dt.UTC),
            # An approved row names its approver. Without this the first insert below
            # fails on that CHECK, and the test stops exercising the unique index it
            # is actually about.
            "approved_by": user_a,
        }
        async with tenant_transaction(engine, tenant_a) as session:
            await session.execute(insert(ProfileField).values(id=new_id(), value="a@example.test", **values))

        with pytest.raises((IntegrityError, DBAPIError)):
            async with tenant_transaction(engine, tenant_a) as session:
                await session.execute(
                    insert(ProfileField).values(id=new_id(), value="b@example.test", **values)
                )

    async def test_several_drafts_of_one_kind_are_fine(
        self, engine: AsyncEngine, tenant_a: TenantId, user_a: UserId
    ) -> None:
        # Two resumes list two phone numbers; the candidate picks one.
        candidate_id = await _candidate(engine, tenant_a, user_a)
        async with tenant_transaction(engine, tenant_a) as session:
            for value in ("+1 555 0100", "+1 555 0101"):
                await session.execute(
                    insert(ProfileField).values(
                        id=new_id(),
                        tenant_id=tenant_a,
                        candidate_id=candidate_id,
                        kind=ProfileFieldKind.PHONE.value,
                        value=value,
                        status=ApprovalStatus.DRAFT.value,
                        source=GraphSource.RESUME_IMPORT.value,
                    )
                )
            count = await session.scalar(
                text("SELECT count(*) FROM profile_fields WHERE candidate_id = :c"),
                {"c": candidate_id},
            )
        assert count == 2

    def test_there_is_no_street_address_kind(self) -> None:
        # SECURITY.md §3 makes a home address S2 and nothing in the product needs
        # one; a form asking for it goes to the user.
        kinds = {kind.value for kind in ProfileFieldKind}
        assert not {"street_address", "address", "address_line_1", "postal_code", "zip"} & kinds
        assert "location_city" in kinds


class TestTenancy:
    async def test_every_graph_table_is_tenant_scoped_and_protected(self, engine: AsyncEngine) -> None:
        # The sweep in test_rls_cross_tenant.py covers the whole schema; this asserts
        # the graph tables are in the declared set, so adding one without a policy
        # fails there rather than leaking.
        assert GRAPH_TENANT_TABLES <= set(Base.metadata.tables)
        for table in GRAPH_TENANT_TABLES:
            assert "tenant_id" in Base.metadata.tables[table].columns

    async def test_a_candidate_is_invisible_to_another_tenant(
        self, engine: AsyncEngine, tenant_a: TenantId, tenant_b: TenantId, user_a: UserId
    ) -> None:
        await _candidate(engine, tenant_a, user_a)
        async with tenant_transaction(engine, tenant_b) as session:
            assert (await session.execute(select(Candidate))).scalars().all() == []
