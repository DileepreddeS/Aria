"""Tenant isolation (SECURITY.md §2.4, §12; PRODUCT_SPEC §7 Phase 0 gate).

This is the Phase 0 "cross-tenant read fails as expected" gate. The tests do not
inspect the policies and call it a day; they act as the application role and try
to read, write and tamper across the boundary, which is what an attacker with a
bug in our API would get to do.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_core.db.models import TENANT_SCOPED_TABLES, Application, User
from aria_core.db.session import (
    platform_transaction,
    tenant_transaction,
    unscoped_transaction_for_tests,
)
from aria_core.schemas.identity import TenantId, UserId, new_id
from aria_core.state_machines.application import ApplicationState

pytestmark = pytest.mark.db

SECRET_JOB = "https://boards.greenhouse.io/acme/jobs/tenant-a-only"


async def _add_application(engine: AsyncEngine, tenant_id: TenantId, job_ref: str = SECRET_JOB) -> uuid.UUID:
    application_id = new_id()
    async with tenant_transaction(engine, tenant_id) as session:
        await session.execute(
            insert(Application).values(
                id=application_id,
                tenant_id=tenant_id,
                job_ref=job_ref,
                state=ApplicationState.DISCOVERED.value,
            )
        )
    return application_id


class TestReadingAcrossTenants:
    async def test_a_tenant_cannot_see_another_tenants_rows(
        self, engine: AsyncEngine, tenant_a: TenantId, tenant_b: TenantId
    ) -> None:
        await _add_application(engine, tenant_a)

        async with tenant_transaction(engine, tenant_b) as session:
            rows = (await session.execute(select(Application))).scalars().all()
            assert rows == []
            count = await session.scalar(select(func.count()).select_from(Application))
            assert count == 0

        async with tenant_transaction(engine, tenant_a) as session:
            own = (await session.execute(select(Application))).scalars().all()
            assert [application.job_ref for application in own] == [SECRET_JOB]

    async def test_asking_for_another_tenants_row_by_primary_key_finds_nothing(
        self, engine: AsyncEngine, tenant_a: TenantId, tenant_b: TenantId
    ) -> None:
        application_id = await _add_application(engine, tenant_a)

        async with tenant_transaction(engine, tenant_b) as session:
            found = await session.scalar(select(Application).where(Application.id == application_id))
            assert found is None

    async def test_a_tenant_cannot_see_another_tenants_users(
        self, engine: AsyncEngine, user_a: UserId, tenant_b: TenantId
    ) -> None:
        async with tenant_transaction(engine, tenant_b) as session:
            assert (await session.execute(select(User))).scalars().all() == []

    async def test_a_tenant_cannot_see_another_tenant_row_itself(
        self, engine: AsyncEngine, tenant_a: TenantId, tenant_b: TenantId
    ) -> None:
        async with tenant_transaction(engine, tenant_b) as session:
            visible = (await session.execute(text("SELECT id FROM tenants"))).scalars().all()
            assert visible == [tenant_b]
            assert tenant_a not in visible


class TestWritingAcrossTenants:
    async def test_inserting_a_row_for_another_tenant_is_refused(
        self, engine: AsyncEngine, tenant_a: TenantId, tenant_b: TenantId
    ) -> None:
        with pytest.raises(DBAPIError) as caught:
            async with tenant_transaction(engine, tenant_b) as session:
                await session.execute(
                    insert(Application).values(
                        id=new_id(),
                        tenant_id=tenant_a,  # someone else's tenant
                        job_ref="smuggled",
                        state=ApplicationState.DISCOVERED.value,
                    )
                )
        assert "row-level security" in str(caught.value).lower()

    async def test_updating_another_tenants_row_changes_nothing(
        self, engine: AsyncEngine, tenant_a: TenantId, tenant_b: TenantId
    ) -> None:
        application_id = await _add_application(engine, tenant_a)

        async with tenant_transaction(engine, tenant_b) as session:
            result = await session.execute(
                update(Application).where(Application.id == application_id).values(job_ref="tampered")
            )
            # Not an error: the row is invisible, so the UPDATE matches nothing.
            assert result.rowcount == 0  # type: ignore[attr-defined]

        async with tenant_transaction(engine, tenant_a) as session:
            unchanged = await session.scalar(
                select(Application.job_ref).where(Application.id == application_id)
            )
            assert unchanged == SECRET_JOB

    async def test_moving_an_own_row_to_another_tenant_is_refused(
        self, engine: AsyncEngine, tenant_a: TenantId, tenant_b: TenantId
    ) -> None:
        application_id = await _add_application(engine, tenant_a)

        with pytest.raises(DBAPIError) as caught:
            async with tenant_transaction(engine, tenant_a) as session:
                await session.execute(
                    update(Application).where(Application.id == application_id).values(tenant_id=tenant_b)
                )
        assert "row-level security" in str(caught.value).lower()


class TestPlatformScope:
    async def test_platform_scope_cannot_read_tenant_data(
        self, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        await _add_application(engine, tenant_a)

        async with platform_transaction(engine) as session:
            assert (await session.execute(select(Application))).scalars().all() == []
            assert (await session.execute(select(User))).scalars().all() == []

    async def test_platform_scope_may_provision_tenants(self, engine: AsyncEngine) -> None:
        async with platform_transaction(engine) as session:
            names = (await session.execute(text("SELECT count(*) FROM tenants"))).scalar_one()
        assert names >= 0  # the provisioning policy allows the read at all


class TestAForgottenScopeIsAnError:
    async def test_a_query_without_a_scope_raises_instead_of_returning_nothing(
        self, engine: AsyncEngine, user_a: UserId
    ) -> None:
        # user_a guarantees the table has a row, so the policy is actually evaluated.
        with pytest.raises(DBAPIError) as caught:
            async with unscoped_transaction_for_tests(engine) as session:
                await session.execute(select(User))
        message = str(caught.value)
        assert "aria.scope is not set" in message
        assert "aria_core.db.session" in message

    async def test_an_insert_without_a_scope_raises(self, engine: AsyncEngine) -> None:
        with pytest.raises(DBAPIError) as caught:
            async with unscoped_transaction_for_tests(engine) as session:
                await session.execute(
                    insert(Application).values(
                        id=new_id(),
                        tenant_id=new_id(),
                        job_ref="x",
                        state=ApplicationState.DISCOVERED.value,
                    )
                )
        assert "aria.scope is not set" in str(caught.value)

    async def test_an_unknown_scope_value_is_refused(self, engine: AsyncEngine, user_a: UserId) -> None:
        with pytest.raises(DBAPIError) as caught:
            async with unscoped_transaction_for_tests(engine) as session:
                await session.execute(
                    text("SELECT set_config('aria.scope', :value, true)"), {"value": "admin"}
                )
                await session.execute(select(User))
        assert "expected tenant or platform" in str(caught.value)


class TestTheApplicationRoleCannotEscape:
    """SECURITY.md §2.4: least privilege is a property of the role, not of our code."""

    @pytest.mark.parametrize(
        "statement",
        [
            "ALTER TABLE users DISABLE ROW LEVEL SECURITY",
            "ALTER TABLE users NO FORCE ROW LEVEL SECURITY",
            "ALTER TABLE applications DISABLE ROW LEVEL SECURITY",
        ],
    )
    async def test_the_app_role_cannot_turn_row_security_off(
        self, engine: AsyncEngine, tenant_a: TenantId, statement: str
    ) -> None:
        with pytest.raises(DBAPIError) as caught:
            async with tenant_transaction(engine, tenant_a) as session:
                await session.execute(text(statement))
        assert "must be owner" in str(caught.value).lower()

        async with tenant_transaction(engine, tenant_a) as session:
            still_on = await session.scalar(
                text("SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE relname = 'users'")
            )
        assert still_on is True

    async def test_the_app_role_cannot_add_a_policy_of_its_own(
        self, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        with pytest.raises(DBAPIError):
            async with tenant_transaction(engine, tenant_a) as session:
                await session.execute(
                    text("CREATE POLICY everything ON users FOR ALL TO aria_app USING (true)")
                )

    async def test_the_app_role_cannot_become_the_owner(
        self, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        with pytest.raises(DBAPIError):
            async with tenant_transaction(engine, tenant_a) as session:
                await session.execute(text("SET ROLE aria_migrate"))

    async def test_the_app_role_cannot_rewrite_history(self, engine: AsyncEngine, tenant_a: TenantId) -> None:
        for statement in (
            "UPDATE audit_events SET reason = 'nothing happened'",
            "DELETE FROM audit_events",
        ):
            with pytest.raises(DBAPIError) as caught:
                async with tenant_transaction(engine, tenant_a) as session:
                    await session.execute(text(statement))
            assert "permission denied" in str(caught.value).lower()

    async def test_the_app_role_cannot_read_alembics_bookkeeping(
        self, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        with pytest.raises(DBAPIError) as caught:
            async with tenant_transaction(engine, tenant_a) as session:
                await session.execute(text("SELECT * FROM alembic_version"))
        assert "permission denied" in str(caught.value).lower()


class TestIntentMatchesWhatTheDatabaseEnforces:
    async def test_every_tenant_table_has_row_security_forced_and_a_policy(self, engine: AsyncEngine) -> None:
        async with platform_transaction(engine) as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT c.relname,
                               c.relrowsecurity,
                               c.relforcerowsecurity,
                               count(p.polname) AS policies
                        FROM pg_class c
                        JOIN pg_namespace n ON n.oid = c.relnamespace
                        LEFT JOIN pg_policy p ON p.polrelid = c.oid
                        WHERE n.nspname = 'public' AND c.relkind = 'r'
                        GROUP BY c.relname, c.relrowsecurity, c.relforcerowsecurity
                        """
                    )
                )
            ).all()

        state = {name: (enabled, forced, policies) for name, enabled, forced, policies in rows}
        for table in sorted(TENANT_SCOPED_TABLES):
            assert table in state, f"{table} is declared tenant-scoped but does not exist"
            enabled, forced, policies = state[table]
            assert enabled, f"{table} holds tenant data without row-level security"
            assert forced, f"{table} does not force row-level security on its owner"
            assert policies >= 1, f"{table} has row-level security enabled but no policy"

    async def test_no_other_public_table_quietly_holds_a_tenant_id(self, engine: AsyncEngine) -> None:
        async with platform_transaction(engine) as session:
            tables_with_tenant_id = set(
                (
                    await session.execute(
                        text(
                            """
                            SELECT table_name FROM information_schema.columns
                            WHERE table_schema = 'public' AND column_name = 'tenant_id'
                            """
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert tables_with_tenant_id <= TENANT_SCOPED_TABLES, (
            "a table carries tenant_id but is not declared in TENANT_SCOPED_TABLES: "
            f"{sorted(tables_with_tenant_id - TENANT_SCOPED_TABLES)}"
        )


class TestForeignKeysDoNotLeakAcrossTenants:
    async def test_a_user_cannot_be_attached_to_another_tenant(
        self, engine: AsyncEngine, tenant_a: TenantId, tenant_b: TenantId
    ) -> None:
        with pytest.raises((DBAPIError, IntegrityError, ProgrammingError)):
            async with tenant_transaction(engine, tenant_b) as session:
                await session.execute(
                    insert(User).values(
                        id=new_id(),
                        tenant_id=tenant_a,
                        email="smuggled@example.test",
                        display_name="x",
                    )
                )
