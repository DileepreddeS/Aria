"""The base class for every typed message between ARIA components (PRODUCT_SPEC §4).

Inter-component messages are never free text. They are versioned Pydantic models
that are immutable once built, reject unknown fields, and classify every field they
carry (SECURITY.md §3).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, ClassVar, Self, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aria_core.sensitivity import Sensitivity, sensitivity_of_annotation

__all__ = ["AriaMessage", "UnclassifiedFieldError", "utc_now"]


def utc_now() -> dt.datetime:
    """Timezone-aware UTC now. Naive datetimes are rejected everywhere in ARIA."""
    return dt.datetime.now(tz=dt.UTC)


class UnclassifiedFieldError(TypeError):
    """A message field did not declare its data class."""


def _nested_message_types(annotation: Any) -> list[type[AriaMessage]]:
    """Message classes reachable from an annotation (through lists, dicts, unions)."""
    found: list[type[AriaMessage]] = []
    stack = [annotation]
    while stack:
        current = stack.pop()
        if isinstance(current, type) and issubclass(current, AriaMessage):
            found.append(current)
            continue
        if get_origin(current) is not None:
            stack.extend(arg for arg in get_args(current) if arg is not type(None))
    return found


class AriaMessage(BaseModel):
    """Immutable, versioned, fully classified message.

    Subclasses set ``SCHEMA_VERSION`` when their shape changes in a way a reader
    could misinterpret. The version travels on the wire in ``schema_version`` so a
    consumer can reject what it does not understand instead of guessing.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        ser_json_timedelta="float",
    )

    SCHEMA_VERSION: ClassVar[int] = 1

    #: Carried so a reader can reject a shape it does not understand.
    schema_version: int = Field(default=0, ge=0)

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        # Not __init_subclass__: that runs before pydantic has collected the fields,
        # so with `from __future__ import annotations` it would see nothing and pass.
        super().__pydantic_init_subclass__(**kwargs)
        cls._assert_every_field_is_classified()

    @classmethod
    def _assert_every_field_is_classified(cls) -> None:
        """Fail at import time if a field carries data of an undeclared class.

        A field whose type is itself a message needs no annotation: its class is the
        strictest class inside it. Everything else must say so explicitly.
        """
        unclassified: list[str] = []
        for name, field in cls.model_fields.items():
            if name == "schema_version":
                continue
            if sensitivity_of_annotation(field.rebuild_annotation()) is not None:
                continue
            if _nested_message_types(field.annotation):
                continue
            unclassified.append(name)
        if unclassified:
            raise UnclassifiedFieldError(
                f"{cls.__module__}.{cls.__qualname__}: field(s) {sorted(unclassified)} do not declare a "
                f"Sensitivity. Annotate them, e.g. `x: Annotated[str, Sensitivity.S1]` (SECURITY.md §3)."
            )

    @model_validator(mode="after")
    def _stamp_schema_version(self) -> Self:
        if self.schema_version == 0:
            # frozen model: set through __dict__ the way pydantic's own validators do
            object.__setattr__(self, "schema_version", type(self).SCHEMA_VERSION)
        return self

    @classmethod
    def field_sensitivity(cls) -> dict[str, Sensitivity]:
        """Every field's data class, with nested messages resolved to their strictest field."""
        result: dict[str, Sensitivity] = {}
        for name, field in cls.model_fields.items():
            if name == "schema_version":
                result[name] = Sensitivity.S0
                continue
            own = sensitivity_of_annotation(field.rebuild_annotation())
            nested = [
                max(inner.field_sensitivity().values(), default=Sensitivity.S0)
                for inner in _nested_message_types(field.annotation)
            ]
            result[name] = max([s for s in (own, *nested) if s is not None], default=Sensitivity.S0)
        return result

    @classmethod
    def max_sensitivity(cls) -> Sensitivity:
        """The strictest class of data this message can carry."""
        return max(cls.field_sensitivity().values(), default=Sensitivity.S0)

    def fields_at_or_above(self, floor: Sensitivity) -> dict[str, Any]:
        """Field name → value for every field classified at ``floor`` or stricter.

        Used by the LLM gateway to reject a payload before it is dispatched, and by
        the audit writer to keep S2/S3 values out of event payloads.
        """
        classes = self.field_sensitivity()
        return {
            name: getattr(self, name)
            for name, sensitivity in classes.items()
            if sensitivity >= floor and getattr(self, name) is not None
        }

    def safe_summary(self) -> dict[str, Any]:
        """A representation that is safe to log: S0 kept, S1 reduced, S2/S3 dropped."""
        summary: dict[str, Any] = {"message": type(self).__name__, "schema_version": self.schema_version}
        for name, sensitivity in self.field_sensitivity().items():
            if name == "schema_version":
                continue
            value = getattr(self, name)
            if sensitivity >= Sensitivity.S2:
                continue
            if sensitivity == Sensitivity.S1:
                summary[name] = _reduce_to_identifier(value)
            else:
                summary[name] = value
        return summary


def _reduce_to_identifier(value: Any) -> Any:
    """S1 in a log line becomes an id or a shape, never the value itself."""
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, list | tuple | set | dict):
        return f"<{type(value).__name__} len={len(value)}>"
    return f"<{type(value).__name__} redacted>"
