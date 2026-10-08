"""State transitions are validated in code and audited (PRODUCT_SPEC §7 Phase 0 gate).

This is the "audit events written for a sample transition" gate. It also covers the
table itself, which is worth testing directly: the lifecycle is a graph, and a
wrong edge is a bug no integration test would obviously catch.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_core.audit.log import read_chain, verify_chain
from aria_core.db.models import Application
from aria_core.db.session import tenant_transaction
from aria_core.schemas.audit import ActorKind, AuditAction, AuditOutcome, SubjectKind
from aria_core.schemas.identity import TenantId, new_id
from aria_core.state_machines.application import ApplicationState as S
from aria_core.state_machines.engine import (
    TRANSITIONS,
    EvidenceRequired,
    IllegalTransition,
    UnknownApplication,
    is_legal,
    transition_application,
)


class TestTheTransitionTable:
    def test_every_state_appears_in_the_table(self) -> None:
        assert set(TRANSITIONS) == set(S)

    def test_every_target_is_a_real_state(self) -> None:
        for source, targets in TRANSITIONS.items():
            assert targets <= set(S), f"{source} points at something that is not a state"

    def test_terminal_states_have_no_moves(self) -> None:
        for state in S:
            if state.is_terminal:
                assert TRANSITIONS[state] == frozenset(), f"{state} is terminal but has moves"

    def test_no_state_transitions_to_itself(self) -> None:
        for state, targets in TRANSITIONS.items():
            assert state not in targets, f"{state} lists itself as a transition"

    def test_anything_before_submission_can_be_cancelled(self) -> None:
        # The kill switch has to stop anything still in flight (PRODUCT_SPEC §3.8).
        # SUBMITTED and FOLLOW_UP are deliberately not cancellable: the application
        # really was sent to an employer, and "cancelled" would misrepresent that.
        # From there the only moves are outcomes.
        after_submission = {S.SUBMITTED, S.FOLLOW_UP}
        for state, targets in TRANSITIONS.items():
            if state.is_terminal or state.is_outcome or state in after_submission:
                continue
            assert S.CANCELLED in targets, f"{state} cannot be cancelled"

    def test_a_submitted_application_can_only_move_to_an_outcome(self) -> None:
        for state in (S.SUBMITTED, S.FOLLOW_UP):
            for target in TRANSITIONS[state]:
                assert target.is_outcome or target is S.FOLLOW_UP, (
                    f"{state} may move to {target}, which neither records an outcome nor follows up"
                )

    def test_every_state_is_reachable_from_discovered(self) -> None:
        seen = {S.DISCOVERED}
        frontier = [S.DISCOVERED]
        while frontier:
            for target in TRANSITIONS[frontier.pop()]:
                if target not in seen:
                    seen.add(target)
                    frontier.append(target)
        assert seen == set(S), f"unreachable: {sorted(state.value for state in set(S) - seen)}"

    def test_submitted_is_only_reachable_through_verifying(self) -> None:
        sources = {state for state, targets in TRANSITIONS.items() if S.SUBMITTED in targets}
        assert sources == {S.VERIFYING}

    @pytest.mark.parametrize(
        ("current", "requested", "expected"),
        [
            (S.DISCOVERED, S.SCREENING, True),
            (S.DISCOVERED, S.SUBMITTED, False),
            (S.READY, S.AWAITING_APPROVAL, True),
            (S.VERIFYING, S.SUBMITTED, True),
            (S.BLOCKED_CAPTCHA, S.APPLYING, True),
            (S.BLOCKED_CLOSED, S.APPLYING, False),
            (S.OUTCOME_OFFER, S.FOLLOW_UP, False),
        ],
    )
    def test_specific_moves(self, current: S, requested: S, expected: bool) -> None:
        assert is_legal(current, requested) is expected


class TestStateProperties:
    def test_states_that_wait_for_a_person_are_marked(self) -> None:
        assert S.BLOCKED_CAPTCHA.needs_user
        assert S.AWAITING_APPROVAL.needs_user
        assert not S.APPLYING.needs_user

    def test_outcome_states_are_recognised(self) -> None:
        assert S.OUTCOME_INTERVIEW.is_outcome
        assert not S.SUBMITTED.is_outcome


@pytest.mark.db
class TestTransitionsAgainstTheDatabase:
    async def _application(self, engine: AsyncEngine, tenant_id: TenantId) -> uuid.UUID:
        application_id = new_id()
        async with tenant_transaction(engine, tenant_id) as session:
            await session.execute(
                insert(Application).values(
                    id=application_id,
                    tenant_id=tenant_id,
                    job_ref="https://jobs.ashbyhq.com/acme/1",
                    state=S.DISCOVERED.value,
                )
            )
        return application_id

    async def _move(
        self,
        engine: AsyncEngine,
        tenant_id: TenantId,
        application_id: uuid.UUID,
        to_state: S,
        **kwargs: object,
    ) -> None:
        async with tenant_transaction(engine, tenant_id) as session:
            await transition_application(
                session,
                tenant_id=tenant_id,
                application_id=application_id,
                to_state=to_state,
                actor_kind=ActorKind.SYSTEM,
                actor_id="test",
                **kwargs,  # type: ignore[arg-type]
            )

    async def test_a_legal_transition_moves_the_row_and_writes_an_event(
        self, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        application_id = await self._application(engine, tenant_a)

        async with tenant_transaction(engine, tenant_a) as session:
            record = await transition_application(
                session,
                tenant_id=tenant_a,
                application_id=application_id,
                to_state=S.SCREENING,
                actor_kind=ActorKind.SYSTEM,
                actor_id="screener",
                reason="daily crawl picked this up",
            )

        assert record.event.action is AuditAction.APPLICATION_STATE_CHANGED
        assert record.event.outcome is AuditOutcome.OK
        assert record.event.subject_kind is SubjectKind.APPLICATION
        assert record.event.subject_id == str(application_id)
        assert record.event.payload == {"from": "discovered", "to": "screening"}
        assert record.event.reason == "daily crawl picked this up"

        async with tenant_transaction(engine, tenant_a) as session:
            state = await session.scalar(select(Application.state).where(Application.id == application_id))
            events = await read_chain(session, tenant_a)

        assert state == S.SCREENING.value
        assert len(events) == 1
        assert events[0].action == AuditAction.APPLICATION_STATE_CHANGED.value

    async def test_a_full_run_leaves_one_event_per_step_in_order(
        self, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        application_id = await self._application(engine, tenant_a)
        path = [
            S.SCREENING,
            S.TAILORING,
            S.VALIDATING,
            S.REPAIRING,
            S.VALIDATING,
            S.READY,
            S.AWAITING_APPROVAL,
            S.DISPATCHED_TO_RUNNER,
            S.APPLYING,
            S.VERIFYING,
        ]
        for state in path:
            await self._move(engine, tenant_a, application_id, state)
        await self._move(
            engine,
            tenant_a,
            application_id,
            S.SUBMITTED,
            evidence_ref="confirmation:https://acme.ashbyhq.com/confirm/abc",
        )

        async with tenant_transaction(engine, tenant_a) as session:
            events = await read_chain(session, tenant_a)
            verification = await verify_chain(session, tenant_a)

        assert len(events) == len(path) + 1
        assert [event.payload["to"] for event in events] == [
            *(state.value for state in path),
            S.SUBMITTED.value,
        ]
        assert verification.valid is True
        assert events[-1].payload["evidence_ref"].startswith("confirmation:")

    async def test_an_illegal_transition_is_refused_and_changes_nothing(
        self, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        application_id = await self._application(engine, tenant_a)

        with pytest.raises(IllegalTransition, match="discovered cannot move to submitted"):
            await self._move(engine, tenant_a, application_id, S.SUBMITTED)

        async with tenant_transaction(engine, tenant_a) as session:
            state = await session.scalar(select(Application.state).where(Application.id == application_id))
            assert state == S.DISCOVERED.value
            # No state change means no event: the two commit together or not at all.
            assert await read_chain(session, tenant_a) == []

    async def test_the_error_says_what_was_allowed(self, engine: AsyncEngine, tenant_a: TenantId) -> None:
        application_id = await self._application(engine, tenant_a)
        with pytest.raises(IllegalTransition, match=r"allowed from here: .*screening"):
            await self._move(engine, tenant_a, application_id, S.APPLYING)

    async def test_a_terminal_state_cannot_be_left(self, engine: AsyncEngine, tenant_a: TenantId) -> None:
        application_id = await self._application(engine, tenant_a)
        await self._move(engine, tenant_a, application_id, S.SKIPPED)

        with pytest.raises(IllegalTransition, match="allowed from here: nothing"):
            await self._move(engine, tenant_a, application_id, S.SCREENING)

    async def test_submitted_without_evidence_is_refused(
        self, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        application_id = await self._application(engine, tenant_a)
        for state in (S.SCREENING, S.TAILORING, S.VALIDATING, S.READY, S.DISPATCHED_TO_RUNNER):
            await self._move(engine, tenant_a, application_id, state)
        await self._move(engine, tenant_a, application_id, S.APPLYING)
        await self._move(engine, tenant_a, application_id, S.VERIFYING)

        with pytest.raises(EvidenceRequired, match="confirmation page"):
            await self._move(engine, tenant_a, application_id, S.SUBMITTED)

        async with tenant_transaction(engine, tenant_a) as session:
            state = await session.scalar(select(Application.state).where(Application.id == application_id))
        assert state == S.VERIFYING.value

    async def test_an_application_in_another_tenant_is_not_found(
        self, engine: AsyncEngine, tenant_a: TenantId, tenant_b: TenantId
    ) -> None:
        application_id = await self._application(engine, tenant_a)

        with pytest.raises(UnknownApplication):
            await self._move(engine, tenant_b, application_id, S.SCREENING)

    async def test_an_unknown_application_is_not_created_by_accident(
        self, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        with pytest.raises(UnknownApplication):
            await self._move(engine, tenant_a, new_id(), S.SCREENING)
