"""The only carrier for content ARIA did not write (SECURITY.md §5).

Job descriptions, web pages, DOM text, emails, PDFs, form questions and recruiter
messages are data. They never become instructions. Everything that reads such
content receives it inside an ``UntrustedContent`` block with its source labelled.

Two properties make the rule hard to break by accident:

* ``str()`` and f-string interpolation of a block yield a short marker, not the
  text, so ``f"Summarise: {content}"`` cannot smuggle a prompt into an instruction.
  Reaching the text takes an explicit ``.text``.
* The LLM gateway accepts untrusted content only in its own request field and
  assembles it into separate, labelled message blocks (see the gateway's
  ``prompt`` module); there is no code path that concatenates it into the
  instruction string.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from aria_core.schemas.base import AriaMessage, utc_now
from aria_core.sensitivity import Sensitivity

__all__ = ["UntrustedContent", "UntrustedSourceKind"]


class UntrustedSourceKind(StrEnum):
    """Where a piece of untrusted content came from. Used as the block's label."""

    JOB_DESCRIPTION = "job_description"
    WEB_PAGE = "web_page"
    PAGE_DOM = "page_dom"
    FORM_QUESTION = "form_question"
    EMAIL = "email"
    RECRUITER_MESSAGE = "recruiter_message"
    UPLOADED_DOCUMENT = "uploaded_document"
    COMPANY_PAGE = "company_page"


class UntrustedContent(AriaMessage):
    """One piece of content from outside ARIA's trust boundary.

    ``text`` is classified S1 rather than S0: a job description is public, but an
    email body or a filled form page can contain the candidate's own personal data,
    and the conservative class is the safe default for a single carrier type. S1
    means "the minimum needed for the task may go to a model, and logs get a shape,
    not the content".
    """

    SCHEMA_VERSION = 1

    source_kind: Annotated[UntrustedSourceKind, Sensitivity.S0]
    #: URL, posting id, message id — enough to find the source again, never a secret.
    source_ref: Annotated[str, Sensitivity.S0] = Field(max_length=2048)
    text: Annotated[str, Sensitivity.S1] = Field(max_length=1_000_000)
    fetched_at: Annotated[dt.datetime, Sensitivity.S0] = Field(default_factory=utc_now)
    #: Set from ``text`` on construction. Lets an audit event name the exact content
    #: a decision was based on without storing the content.
    content_sha256: Annotated[str, Sensitivity.S0] = ""

    @model_validator(mode="after")
    def _digest(self) -> Self:
        digest = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        if not self.content_sha256:
            object.__setattr__(self, "content_sha256", digest)
        elif self.content_sha256 != digest:
            raise ValueError("content_sha256 does not match text")
        if self.fetched_at.tzinfo is None:
            raise ValueError("fetched_at must be timezone-aware")
        return self

    @property
    def marker(self) -> str:
        """What this block looks like in a log line or an accidental f-string."""
        return (
            f"<UntrustedContent {self.source_kind.value} "
            f"bytes={len(self.text.encode('utf-8'))} sha256={self.content_sha256[:12]}>"
        )

    def __str__(self) -> str:
        return self.marker

    def __repr__(self) -> str:
        return self.marker

    def __format__(self, format_spec: str) -> str:
        return self.marker
