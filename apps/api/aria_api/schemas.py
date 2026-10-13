"""Request and response bodies for the API.

Plain Pydantic models rather than :class:`~aria_core.schemas.base.AriaMessage`: these
are the shape of the HTTP surface, not internal messages, and the sensitivity rules
that ``AriaMessage`` enforces at class-definition time are about what may reach a
model or a log line. What may reach *this* client is a separate question, and the
answer is "their own data", since the only caller is the candidate themselves.

One rule does carry over: nothing here returns an id without the text it refers to.
A list of fact ids is useless to a person deciding what to approve, and an approval
flow that shows less than the claim being approved is not an approval flow.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AnswerRequest",
    "AnswerResponse",
    "ClassifiedLineBody",
    "CompletenessResponse",
    "DraftFact",
    "DraftList",
    "EditRequest",
    "EntityList",
    "EntityOut",
    "GapList",
    "GapOut",
    "ImportResponse",
    "InterviewResponse",
    "LinesResponse",
    "QuestionList",
    "QuestionOut",
    "RejectRequest",
    "UploadResponse",
]


class Body(BaseModel):
    """Strict, and no extra fields.

    An unexpected field is a client that thinks it is doing something this version
    does not do, which is better reported than ignored.
    """

    model_config = ConfigDict(extra="forbid", strict=False)


# ------------------------------------------------------------------------ uploads


class UploadResponse(Body):
    upload_id: uuid.UUID
    detected_type: str | None = None
    parse_status: str
    #: True when a person should check this upload against the original.
    needs_review: bool = False
    #: Pages whose columns were found by geometry and whose text was rebuilt in
    #: reading order. Worth checking: the gutter settles which column comes first,
    #: not whether a sidebar of skills belongs where it landed.
    column_pages: list[int] = Field(default_factory=list)
    extractor: str = ""
    characters: int = 0
    links: list[str] = Field(default_factory=list)
    detail: str = ""
    #: Set when the file was turned away. The row and the audit event still exist.
    refusal_reason: str | None = None
    refusal_advice: str | None = None
    #: Set when these exact bytes were already uploaded. The id is the earlier
    #: upload's, so the client can import it without uploading again.
    duplicate_of: uuid.UUID | None = None

    @property
    def accepted(self) -> bool:
        return self.refusal_reason is None


# ------------------------------------------------------------------------ imports


class ImportResponse(Body):
    upload_id: uuid.UUID | None
    #: "imported", or "needs_user_fix" when a §5.5 gate refused the document and
    #: nothing was written.
    status: str = "imported"
    counts: dict[str, int]
    #: What the import proposed and wrote, in the words the CLI prints.
    summary: list[str]
    questions_queued: int = 0
    held_back: list[str] = Field(default_factory=list)
    #: Why the document was refused, for the candidate. Empty when imported.
    reasons: list[str] = Field(default_factory=list)
    #: Entries and bullets left out because they did not have the shape of one, with
    #: the line each came from. Quotes the resume: S1, the candidate's own text.
    dropped: list[str] = Field(default_factory=list)
    #: Imported, but worth a look: a start date more than a year ahead, say.
    flags: list[str] = Field(default_factory=list)


class ClassifiedLineBody(Body):
    #: 1-based, the same numbering as a fact's source_line.
    number: int
    page: int
    column: int
    kind: str
    text: str
    #: For an entry header, the date read from its right-hand band.
    date_text: str = ""


class LinesResponse(Body):
    """How ARIA read a PDF, line by line: what the import was built from."""

    upload_id: uuid.UUID
    lines: list[ClassifiedLineBody]
    #: Pages whose column order the geometry could not settle.
    ambiguous_pages: list[int] = Field(default_factory=list)


# ------------------------------------------------------------------------- drafts


class DraftFact(Body):
    """A draft fact with everything needed to decide on it.

    The source upload and line travel with it because approving a fact is a truth
    claim: the candidate sees the line of their own resume it came from.
    """

    fact_id: uuid.UUID
    text: str
    version: int
    status: str
    source: str
    source_upload_id: uuid.UUID | None = None
    source_line: int | None = None
    experience_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    education_id: uuid.UUID | None = None


class DraftList(Body):
    facts: list[DraftFact]
    total: int


class RejectRequest(Body):
    reason: str = Field(default="", max_length=500)


class EditRequest(Body):
    text: str = Field(min_length=1, max_length=4000)
    #: An edit is the candidate writing the sentence themselves, so it is approved by
    #: default. Pass false to keep it a draft.
    approve: bool = True


# ----------------------------------------------------------------- graph entities


class EntityOut(Body):
    """A graph row with enough of it to decide on.

    ``summary`` is the row in one line. A list of ids is not something anybody can
    approve, which is the same reason DraftFact carries its text.
    """

    entity_id: uuid.UUID
    kind: str
    status: str
    summary: str
    source: str
    source_line: int | None = None
    source_upload_id: uuid.UUID | None = None
    approved_by: uuid.UUID | None = None


class EntityList(Body):
    kind: str
    entities: list[EntityOut]
    total: int


# ---------------------------------------------------------------------- questions


class QuestionOut(Body):
    question_id: uuid.UUID
    kind: str
    prompt: str
    detail: str
    status: str
    source_upload_id: uuid.UUID | None = None
    source_lines: list[int] = Field(default_factory=list)
    subject_kind: str | None = None
    subject_id: uuid.UUID | None = None
    answer: str | None = None
    created_at: dt.datetime


class QuestionList(Body):
    questions: list[QuestionOut]
    open_count: int


class AnswerRequest(Body):
    answer: str = Field(min_length=1, max_length=2000)


class AnswerResponse(Body):
    question_id: uuid.UUID
    #: What the answer created or changed, if anything.
    subject_kind: str | None = None
    subject_id: uuid.UUID | None = None
    fact_ids: list[uuid.UUID] = Field(default_factory=list)
    detail: str = ""
    summary: str = ""


# --------------------------------------------------------------------- interviews


class GapOut(Body):
    """Something missing from the graph, and the question that would fill it."""

    kind: str
    prompt: str
    detail: str
    priority: int
    subject_kind: str | None = None
    subject_id: uuid.UUID | None = None


class GapList(Body):
    gaps: list[GapOut]
    total: int


class InterviewResponse(Body):
    interview: str
    queued: int
    #: The questions newly put on the queue, so the client can show them without a
    #: second request.
    prompts: list[str] = Field(default_factory=list)
    remaining_gaps: int = 0
    summary: str = ""


# --------------------------------------------------------------------- the report


class CompletenessResponse(Body):
    """How complete the graph is, measurably.

    "Graph complete" is defined in :mod:`aria_api.routers.report` rather than left to
    a feeling: a definition nobody can check is not a gate.
    """

    complete: bool
    #: Each requirement, whether it is met, and what is missing.
    checks: list[dict[str, Any]]
    counts: dict[str, int]
    summary: list[str]
