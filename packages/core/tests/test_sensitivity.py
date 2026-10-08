"""Data classification is enforced by construction (SECURITY.md §3)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

import pytest
from pydantic import ValidationError

from aria_core.schemas.base import AriaMessage, UnclassifiedFieldError, utc_now
from aria_core.sensitivity import Sensitivity, sensitivity_of_annotation


class Contact(AriaMessage):
    SCHEMA_VERSION = 2

    tenant_id: Annotated[uuid.UUID, Sensitivity.S0]
    full_name: Annotated[str, Sensitivity.S1]
    visa_status: Annotated[str, Sensitivity.S2]
    session_cookie: Annotated[str | None, Sensitivity.S3] = None


class Envelope(AriaMessage):
    label: Annotated[str, Sensitivity.S0]
    contact: Contact


def _contact() -> Contact:
    return Contact(
        tenant_id=uuid.uuid4(),
        full_name="Dileep Kumar Salla",
        visa_status="F-1 OPT, EAD valid to 2027-05-31",
    )


class TestClassificationIsMandatory:
    def test_a_field_without_a_class_fails_at_class_definition(self) -> None:
        with pytest.raises(UnclassifiedFieldError, match="do not declare a Sensitivity"):

            class Forgot(AriaMessage):
                home_address: str

    def test_a_nested_message_needs_no_annotation_of_its_own(self) -> None:
        assert Envelope.field_sensitivity()["contact"] is Sensitivity.S3

    def test_a_container_is_as_sensitive_as_what_it_holds(self) -> None:
        assert sensitivity_of_annotation(list[Annotated[str, Sensitivity.S2]] | None) is Sensitivity.S2

    def test_the_strictest_class_wins_when_several_are_declared(self) -> None:
        assert sensitivity_of_annotation(Annotated[str, Sensitivity.S1, Sensitivity.S3]) is Sensitivity.S3


class TestHandlingRules:
    @pytest.mark.parametrize(
        ("sensitivity", "to_llm", "to_logs", "to_frontend"),
        [
            (Sensitivity.S0, True, True, True),
            (Sensitivity.S1, True, True, True),
            (Sensitivity.S2, False, False, False),
            (Sensitivity.S3, False, False, False),
        ],
    )
    def test_rules_match_the_security_doc(
        self, sensitivity: Sensitivity, to_llm: bool, to_logs: bool, to_frontend: bool
    ) -> None:
        assert sensitivity.may_reach_llm is to_llm
        assert sensitivity.may_appear_in_logs is to_logs
        assert sensitivity.may_reach_frontend is to_frontend

    def test_classes_are_ordered_so_a_floor_can_be_compared(self) -> None:
        assert Sensitivity.S0 < Sensitivity.S1 < Sensitivity.S2 < Sensitivity.S3


class TestSensitiveValuesDoNotLeakThroughSerialization:
    def test_safe_summary_drops_s2_and_s3_and_reduces_s1(self) -> None:
        contact = _contact()
        summary = contact.safe_summary()

        assert "visa_status" not in summary
        assert "session_cookie" not in summary
        assert summary["full_name"] == "<str redacted>"
        assert summary["tenant_id"] == contact.tenant_id

        rendered = repr(summary)
        assert "F-1 OPT" not in rendered
        assert "Dileep" not in rendered

    def test_fields_at_or_above_finds_what_must_not_be_dispatched(self) -> None:
        contact = _contact()
        assert set(contact.fields_at_or_above(Sensitivity.S2)) == {"visa_status"}
        assert set(contact.fields_at_or_above(Sensitivity.S1)) == {"full_name", "visa_status"}

    def test_max_sensitivity_sees_through_nesting(self) -> None:
        assert Contact.max_sensitivity() is Sensitivity.S3
        assert Envelope.max_sensitivity() is Sensitivity.S3


class TestMessageDiscipline:
    def test_messages_are_immutable(self) -> None:
        contact = _contact()
        with pytest.raises(ValidationError):
            contact.full_name = "someone else"  # type: ignore[misc]

    def test_unknown_fields_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Contact(
                tenant_id=uuid.uuid4(),
                full_name="x",
                visa_status="y",
                ssn="123-45-6789",  # type: ignore[call-arg]
            )

    def test_the_schema_version_travels_with_the_message(self) -> None:
        assert _contact().schema_version == 2
        assert Contact.model_validate_json(_contact().model_dump_json()).schema_version == 2

    def test_strict_mode_refuses_to_coerce(self) -> None:
        with pytest.raises(ValidationError):
            Contact(tenant_id="not-a-uuid", full_name="x", visa_status="y")  # type: ignore[arg-type]

    def test_json_input_still_accepts_the_only_representation_json_has(self) -> None:
        payload = _contact().model_dump_json()
        assert isinstance(Contact.model_validate_json(payload).tenant_id, uuid.UUID)


def test_utc_now_is_timezone_aware() -> None:
    assert utc_now().tzinfo is dt.UTC
