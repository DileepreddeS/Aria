"""The policy engine decides; the model only proposes (SECURITY.md §4)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_core.audit.log import read_chain
from aria_core.db.session import tenant_transaction
from aria_core.policy.engine import RULES, decide_and_audit, evaluate
from aria_core.policy.rules.navigate import check as navigate_check
from aria_core.policy.rules.navigate import host_is_allowed
from aria_core.schemas.audit import AuditAction, AuditOutcome
from aria_core.schemas.identity import TenantId
from aria_core.schemas.policy import (
    AutonomyLevel,
    Capability,
    Decision,
    PolicyContext,
    PolicyDecision,
    PolicyRequest,
    ReasonCode,
)

ATS_HOST = "boards.greenhouse.io"


def _request(
    capability: Capability,
    *,
    context: PolicyContext | None = None,
    **arguments: str,
) -> PolicyRequest:
    return PolicyRequest(
        capability=capability,
        actor_id="planner",
        subject_id=str(uuid.uuid4()),
        arguments=arguments,
        context=context or PolicyContext(autonomy_level=AutonomyLevel.AUTOPILOT),
    )


def _navigate_context(**overrides: object) -> PolicyContext:
    defaults: dict[str, object] = {
        "autonomy_level": AutonomyLevel.AUTOPILOT,
        "granted_capabilities": frozenset({Capability.NAVIGATE}),
        "allowed_hosts": (ATS_HOST, ".greenhouse.io"),
    }
    return PolicyContext(**(defaults | overrides))  # type: ignore[arg-type]


def _submit_context(**overrides: object) -> PolicyContext:
    defaults: dict[str, object] = {
        "autonomy_level": AutonomyLevel.AUTOPILOT,
        "granted_capabilities": frozenset({Capability.SUBMIT_APPLICATION}),
        "required_fields_resolved": True,
        "posting_open": True,
        "open_user_questions": 0,
        "within_apply_window": True,
        "applications_today": 3,
        "daily_cap": 10,
        "applications_for_company_today": 0,
        "company_cap": 2,
    }
    return PolicyContext(**(defaults | overrides))  # type: ignore[arg-type]


def _public_resolver(_: str) -> list[str]:
    return ["93.184.216.34"]


class TestDefaultDeny:
    @pytest.mark.parametrize(
        "capability",
        [
            Capability.FILL_FIELD,
            Capability.UPLOAD_RESUME,
            Capability.USE_CREDENTIAL,
            Capability.CREATE_ACCOUNT,
            Capability.READ_EMAIL_SUMMARY,
            Capability.SEND_EMAIL,
            Capability.READ_JOB,
            Capability.READ_APPLICATION,
        ],
    )
    def test_a_capability_with_no_rule_is_refused(self, capability: Capability) -> None:
        # Adding a capability to the enum must not permit it. The rule has to be
        # written deliberately.
        decision = evaluate(
            _request(
                capability,
                context=PolicyContext(
                    autonomy_level=AutonomyLevel.AUTOPILOT,
                    granted_capabilities=frozenset({capability}),
                ),
            )
        )
        assert decision.decision is Decision.DENY
        assert decision.reason_code is ReasonCode.NO_RULE

    def test_send_email_is_never_granted_in_v1(self) -> None:
        assert Capability.SEND_EMAIL not in RULES

    def test_only_the_two_phase_0_rules_are_registered(self) -> None:
        assert set(RULES) == {Capability.NAVIGATE, Capability.SUBMIT_APPLICATION}

    def test_a_capability_the_task_was_not_granted_is_refused(self) -> None:
        decision = evaluate(
            _request(
                Capability.NAVIGATE,
                context=_navigate_context(granted_capabilities=frozenset()),
                url=f"https://{ATS_HOST}/acme/jobs/1",
            )
        )
        assert decision.reason_code is ReasonCode.CAPABILITY_NOT_GRANTED

    def test_the_kill_switch_stops_everything(self) -> None:
        decision = evaluate(
            _request(
                Capability.NAVIGATE,
                context=_navigate_context(kill_switch_engaged=True),
                url=f"https://{ATS_HOST}/acme/jobs/1",
            )
        )
        assert decision.reason_code is ReasonCode.KILL_SWITCH
        # Checked before anything else, so an otherwise valid request still stops.
        assert decision.checks_passed == ()


class TestNavigate:
    def _decide(self, url: str, **overrides: object) -> PolicyDecision:
        return navigate_check(
            _request(Capability.NAVIGATE, context=_navigate_context(**overrides), url=url),
            resolve=_public_resolver,
        )

    def test_an_allowlisted_https_host_is_allowed(self) -> None:
        decision = self._decide(f"https://{ATS_HOST}/acme/jobs/1?token=abc")
        assert decision.allowed
        assert "addresses_global" in decision.checks_passed

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("http://boards.greenhouse.io/acme", ReasonCode.SCHEME_NOT_ALLOWED),
            ("file:///c:/windows/system32", ReasonCode.SCHEME_NOT_ALLOWED),
            ("javascript:alert(1)", ReasonCode.SCHEME_NOT_ALLOWED),
            ("ftp://boards.greenhouse.io/", ReasonCode.SCHEME_NOT_ALLOWED),
            ("https://user:pass@boards.greenhouse.io/", ReasonCode.CREDENTIALS_IN_URL),
            ("https://boards.greenhouse.io:8080/", ReasonCode.PORT_NOT_ALLOWED),
            ("https://evil.test/acme", ReasonCode.HOST_NOT_ALLOWED),
            ("https://boards.greenhouse.io.evil.test/", ReasonCode.HOST_NOT_ALLOWED),
            ("https:///no-host", ReasonCode.MALFORMED_ARGUMENT),
        ],
    )
    def test_refusals(self, url: str, expected: ReasonCode) -> None:
        assert self._decide(url).reason_code is expected

    def test_a_missing_url_is_refused(self) -> None:
        decision = navigate_check(_request(Capability.NAVIGATE, context=_navigate_context()))
        assert decision.reason_code is ReasonCode.MALFORMED_ARGUMENT

    @pytest.mark.parametrize(
        "literal",
        [
            "127.0.0.1",
            "10.0.0.5",
            "172.16.0.1",
            "192.168.1.1",
            "169.254.169.254",  # cloud metadata
            "[::1]",
            "[fd00::1]",
            "100.64.0.1",  # carrier-grade NAT
            "0.0.0.0",  # noqa: S104 — the unspecified address, refused like the rest
        ],
    )
    def test_a_private_or_reserved_literal_address_is_refused(self, literal: str) -> None:
        # The host allowlist has to contain it for the address check to be what
        # refuses it; otherwise this would only be testing the allowlist.
        decision = self._decide(f"https://{literal}/", allowed_hosts=(literal.strip("[]"),))
        assert decision.reason_code is ReasonCode.PRIVATE_ADDRESS

    @pytest.mark.parametrize(
        "address",
        ["127.0.0.1", "169.254.169.254", "::1", "10.1.2.3"],
    )
    def test_a_name_that_resolves_to_a_private_address_is_refused(self, address: str) -> None:
        # DNS rebinding and "localhost in a CNAME" both look like this.
        decision = navigate_check(
            _request(
                Capability.NAVIGATE,
                context=_navigate_context(),
                url=f"https://{ATS_HOST}/",
            ),
            resolve=lambda _: [address],
        )
        assert decision.reason_code is ReasonCode.PRIVATE_ADDRESS

    def test_one_private_address_among_several_is_enough_to_refuse(self) -> None:
        decision = navigate_check(
            _request(Capability.NAVIGATE, context=_navigate_context(), url=f"https://{ATS_HOST}/"),
            resolve=lambda _: ["93.184.216.34", "127.0.0.1"],
        )
        assert decision.reason_code is ReasonCode.PRIVATE_ADDRESS

    def test_a_host_that_does_not_resolve_is_refused(self) -> None:
        decision = navigate_check(
            _request(Capability.NAVIGATE, context=_navigate_context(), url=f"https://{ATS_HOST}/"),
            resolve=lambda _: [],
        )
        assert decision.reason_code is ReasonCode.UNRESOLVABLE_HOST


class TestHostAllowlist:
    def test_an_exact_entry_does_not_match_a_subdomain(self) -> None:
        # login.acme.com is a different host serving different content.
        assert host_is_allowed("acme.com", ("acme.com",))
        assert not host_is_allowed("login.acme.com", ("acme.com",))

    def test_a_dotted_entry_matches_subdomains_and_the_apex(self) -> None:
        assert host_is_allowed("boards.greenhouse.io", (".greenhouse.io",))
        assert host_is_allowed("greenhouse.io", (".greenhouse.io",))

    def test_a_suffix_cannot_be_faked_by_appending_a_domain(self) -> None:
        assert not host_is_allowed("greenhouse.io.evil.test", (".greenhouse.io",))

    def test_matching_ignores_case_and_a_trailing_dot(self) -> None:
        assert host_is_allowed("Boards.Greenhouse.IO.", (".greenhouse.io",))

    def test_an_empty_allowlist_allows_nothing(self) -> None:
        assert not host_is_allowed("boards.greenhouse.io", ())


class TestSubmitApplication:
    def _decide(self, **overrides: object) -> PolicyDecision:
        return evaluate(_request(Capability.SUBMIT_APPLICATION, context=_submit_context(**overrides)))

    def test_autopilot_with_everything_resolved_is_allowed(self) -> None:
        assert self._decide().allowed

    def test_suggest_never_submits(self) -> None:
        decision = self._decide(autonomy_level=AutonomyLevel.SUGGEST)
        assert decision.reason_code is ReasonCode.AUTONOMY_FORBIDS

    def test_approve_mode_needs_an_approval(self) -> None:
        decision = self._decide(autonomy_level=AutonomyLevel.APPROVE)
        assert decision.reason_code is ReasonCode.APPROVAL_REQUIRED
        assert self._decide(autonomy_level=AutonomyLevel.APPROVE, approval_granted=True).allowed

    def test_a_dream_company_needs_an_approval_even_on_autopilot(self) -> None:
        decision = self._decide(is_dream_company=True)
        assert decision.reason_code is ReasonCode.APPROVAL_REQUIRED
        assert "dream company" in decision.reason
        assert self._decide(is_dream_company=True, approval_granted=True).allowed

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({"open_user_questions": 1}, ReasonCode.OPEN_USER_QUESTIONS),
            ({"required_fields_resolved": False}, ReasonCode.FIELDS_UNRESOLVED),
            ({"posting_open": False}, ReasonCode.POSTING_CLOSED),
            ({"applications_today": 10, "daily_cap": 10}, ReasonCode.DAILY_CAP_REACHED),
            (
                {"applications_for_company_today": 2, "company_cap": 2},
                ReasonCode.COMPANY_CAP_REACHED,
            ),
            ({"within_apply_window": False}, ReasonCode.OUTSIDE_APPLY_WINDOW),
        ],
    )
    def test_each_precondition_refuses_on_its_own(
        self, overrides: dict[str, object], expected: ReasonCode
    ) -> None:
        assert self._decide(**overrides).reason_code is expected

    def test_an_open_question_outranks_a_resolved_form(self) -> None:
        # ARIA never submits over an unanswered question, even if the form looks
        # complete: the answer might change the application.
        decision = self._decide(open_user_questions=2, required_fields_resolved=True)
        assert decision.reason_code is ReasonCode.OPEN_USER_QUESTIONS

    def test_a_cap_of_zero_means_no_cap_rather_than_no_applications(self) -> None:
        assert self._decide(daily_cap=0, applications_today=500).allowed

    def test_an_allow_records_which_checks_passed(self) -> None:
        decision = self._decide()
        assert decision.checks_passed == (
            "autonomy",
            "no_open_questions",
            "fields_resolved",
            "posting_open",
            "daily_cap",
            "company_cap",
            "apply_window",
        )


class TestDecisionsAreNotAdvisory:
    def test_a_denial_can_be_turned_into_an_exception(self) -> None:
        decision = evaluate(_request(Capability.SEND_EMAIL))
        with pytest.raises(PermissionError, match="send_email denied"):
            decision.raise_if_denied()

    def test_an_allow_raises_nothing(self) -> None:
        evaluate(_request(Capability.SUBMIT_APPLICATION, context=_submit_context())).raise_if_denied()


@pytest.mark.db
class TestEveryDecisionIsAudited:
    async def test_a_denial_is_recorded_with_its_reason_code(
        self, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        request = _request(
            Capability.NAVIGATE,
            context=_navigate_context(),
            url="https://evil.test/collect",
        )
        async with tenant_transaction(engine, tenant_a) as session:
            decision = await decide_and_audit(session, request, tenant_id=tenant_a)

        assert decision.reason_code is ReasonCode.HOST_NOT_ALLOWED

        async with tenant_transaction(engine, tenant_a) as session:
            events = await read_chain(session, tenant_a)

        assert len(events) == 1
        assert events[0].action == AuditAction.POLICY_DECIDED.value
        assert events[0].outcome == AuditOutcome.DENIED.value
        assert events[0].payload["reason_code"] == "host_not_allowed"
        assert events[0].payload["arg.url"] == "https://evil.test/collect"

    async def test_an_allow_is_recorded_too(self, engine: AsyncEngine, tenant_a: TenantId) -> None:
        # An audit log holding only refusals cannot answer "why did it submit that?".
        request = _request(Capability.SUBMIT_APPLICATION, context=_submit_context())
        async with tenant_transaction(engine, tenant_a) as session:
            decision = await decide_and_audit(session, request, tenant_id=tenant_a)
        assert decision.allowed

        async with tenant_transaction(engine, tenant_a) as session:
            events = await read_chain(session, tenant_a)

        assert events[0].outcome == AuditOutcome.ALLOWED.value
        assert events[0].payload["capability"] == "submit_application"
        assert "apply_window" in events[0].payload["checks_passed"]
