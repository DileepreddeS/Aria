"""The layout Stage 0 hands from the sandboxed parser to core (aria_core.documents).

It crosses a process boundary from a worker that reads hostile files, so it is
validated on arrival: a subverted worker may send a well-formed layout of a document
that does not exist, but never a malformed one, and never a confidence score.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_core.db.models import Candidate, Upload
from aria_core.db.session import tenant_transaction
from aria_core.documents import (
    CrossExtractor,
    LayoutDocument,
    LayoutLine,
    LayoutPage,
    Triage,
    WeightSource,
)
from aria_core.schemas.identity import TenantId, UserId, new_id
from aria_core.sensitivity import Sensitivity


def _line(index: int, page: int = 1, **overrides: Any) -> dict[str, Any]:
    line: dict[str, Any] = {
        "index": index,
        "page": page,
        "column": 0,
        "x0": 50.0,
        "x1": 300.0,
        "top": 100.0 + 14 * index,
        "bottom": 110.0 + 14 * index,
        "text_x0": 58.0,
        "text": f"Line {index}",
        "font_size": 10.5,
        "bold": False,
        "weight_source": "font_name",
    }
    line.update(overrides)
    if "segments" not in overrides:
        line["segments"] = [
            {
                "text": line["text"],
                "x0": line["text_x0"],
                "x1": line["x1"],
                "font_size": line["font_size"],
                "bold": line["bold"],
            }
        ]
    return line


def _page(number: int = 1, **overrides: Any) -> dict[str, Any]:
    page: dict[str, Any] = {
        "number": number,
        "width": 612.0,
        "height": 792.0,
        "character_count": 2400,
        "unreadable_share": 0.0,
        "body_size": 10.5,
    }
    page.update(overrides)
    return page


def _document(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "triage": "readable",
        "pages": [_page()],
        "lines": [_line(0), _line(1)],
        "cross_extractor": "agree",
    }
    document.update(overrides)
    return document


def _from_worker(data: dict[str, Any]) -> LayoutDocument:
    """As it arrives: JSON from another process."""
    import json

    return LayoutDocument.model_validate_json(json.dumps(data))


class TestWhatArrives:
    def test_a_readable_document_round_trips(self) -> None:
        document = _from_worker(_document())
        assert LayoutDocument.model_validate_json(document.model_dump_json()) == document
        assert document.lines[1].weight_source is WeightSource.FONT_NAME

    def test_a_refusal_carries_its_reason_and_no_lines(self) -> None:
        document = _from_worker({"triage": "no_text_layer", "triage_detail": "no text layer: a scan"})
        assert document.triage is Triage.NO_TEXT_LAYER
        assert document.lines == ()

    def test_a_refusal_with_lines_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="carries no lines"):
            _from_worker({"triage": "locked", "triage_detail": "locked", "lines": [_line(0)]})

    def test_a_refusal_without_a_reason_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="must say why"):
            _from_worker({"triage": "locked"})

    def test_a_readable_document_must_record_the_cross_check(self) -> None:
        with pytest.raises(ValidationError, match="cross-extractor"):
            _from_worker(_document(cross_extractor=None))


class TestNoScoreCanBeSmuggledIn:
    def test_the_diagnostic_has_two_values_and_nothing_numeric(self) -> None:
        assert {member.value for member in CrossExtractor} == {"agree", "disagree"}

    def test_an_agreement_percentage_is_an_unknown_field(self) -> None:
        with pytest.raises(ValidationError, match="agreement"):
            _from_worker(_document(agreement=1.0))

    def test_the_diagnostic_rejects_anything_but_its_two_words(self) -> None:
        with pytest.raises(ValidationError):
            _from_worker(_document(cross_extractor="100%"))


class TestTheLinesAreCoherent:
    def test_indexes_run_in_reading_order_from_zero(self) -> None:
        with pytest.raises(ValidationError, match=r"0\.\.n-1"):
            _from_worker(_document(lines=[_line(0), _line(2)]))

    def test_pages_are_numbered_from_one(self) -> None:
        with pytest.raises(ValidationError, match=r"1\.\.n"):
            _from_worker(_document(pages=[_page(2)], lines=[_line(0, page=2)]))

    def test_a_line_on_a_page_that_is_not_there_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="not there"):
            _from_worker(_document(lines=[_line(0), _line(1, page=3)]))

    def test_an_inverted_box_is_refused(self) -> None:
        # "inverted box", not just "inverted": a flipped line also flips its segment,
        # and the segment check alone would make a looser match pass.
        with pytest.raises(ValidationError, match="inverted box"):
            _from_worker(_document(lines=[_line(0, x0=300.0, x1=50.0, text_x0=60.0)]))

    def test_text_cannot_start_outside_its_own_line(self) -> None:
        with pytest.raises(ValidationError, match="outside its own box"):
            _from_worker(_document(lines=[_line(0, text_x0=400.0)]))

    def test_a_header_and_its_date_travel_as_two_segments(self) -> None:
        header = _line(
            0,
            text="Nordvale Consulting Feb 2024",
            x1=560.0,
            bold=True,
            rule_below=False,
            segments=[
                {"text": "Nordvale Consulting", "x0": 58.0, "x1": 160.0, "font_size": 10.5, "bold": True},
                {"text": "Feb 2024", "x0": 500.0, "x1": 560.0, "font_size": 10.5, "bold": False},
            ],
        )
        line = _from_worker(_document(lines=[header])).lines[0]
        assert [segment.bold for segment in line.segments] == [True, False]

    def test_a_line_must_be_its_segments(self) -> None:
        with pytest.raises(ValidationError, match="not its segments"):
            _from_worker(
                _document(
                    lines=[
                        _line(
                            0,
                            text="Built a compiler",
                            segments=[
                                {
                                    "text": "Built a parser",
                                    "x0": 58.0,
                                    "x1": 300.0,
                                    "font_size": 10.5,
                                    "bold": False,
                                }
                            ],
                        )
                    ]
                )
            )

    def test_segments_run_left_to_right_inside_the_line(self) -> None:
        backwards = [
            {"text": "Feb 2024", "x0": 250.0, "x1": 300.0, "font_size": 10.5, "bold": False},
            {"text": "Nordvale", "x0": 58.0, "x1": 120.0, "font_size": 10.5, "bold": True},
        ]
        with pytest.raises(ValidationError, match="out of order"):
            _from_worker(_document(lines=[_line(0, text="Feb 2024 Nordvale", segments=backwards)]))

    def test_a_line_has_at_least_one_segment(self) -> None:
        with pytest.raises(ValidationError):
            _from_worker(_document(lines=[_line(0, segments=[])]))

    def test_gutters_match_the_column_count(self) -> None:
        with pytest.raises(ValidationError, match="gutters"):
            _from_worker(_document(pages=[_page(columns=2)]))
        assert (
            _from_worker(_document(pages=[_page(columns=2, gutters=[[300.0, 320.0]])])).pages[0].columns == 2
        )


class TestClassification:
    def test_resume_text_is_s1_and_geometry_is_s0(self) -> None:
        line = LayoutLine.field_sensitivity()
        assert line["text"] is Sensitivity.S1
        assert line["x0"] is line["top"] is line["font_size"] is Sensitivity.S0
        assert LayoutPage.field_sensitivity()["running_heads"] is Sensitivity.S1
        document = LayoutDocument.field_sensitivity()
        assert document["links"] is Sensitivity.S1
        # A document is as sensitive as the strictest thing it holds: the lines.
        assert document["lines"] is Sensitivity.S1
        assert document["cross_extractor"] is Sensitivity.S0


# --------------------------------------------------------------- the stored copy


@pytest.fixture
async def upload_id(engine: AsyncEngine, tenant_a: TenantId, user_a: UserId) -> uuid.UUID:
    candidate, upload = new_id(), new_id()
    async with tenant_transaction(engine, tenant_a) as session:
        await session.execute(insert(Candidate).values(id=candidate, tenant_id=tenant_a, user_id=user_a))
        await session.execute(
            insert(Upload).values(
                id=upload,
                tenant_id=tenant_a,
                candidate_id=candidate,
                original_filename="resume.pdf",
                detected_type="pdf",
                size_bytes=4096,
                sha256=upload.hex * 2,
                quarantine_name=f"{upload}.bin",
                scan_status="not_scanned",
                parse_status="parsed",
            )
        )
    return upload


@pytest.mark.db
class TestTheUploadKeepsTheLayout:
    async def test_the_layout_and_the_diagnostic_are_stored(
        self, engine: AsyncEngine, tenant_a: TenantId, upload_id: uuid.UUID
    ) -> None:
        document = _from_worker(_document())
        async with tenant_transaction(engine, tenant_a) as session:
            await session.execute(
                update(Upload)
                .where(Upload.id == upload_id)
                .values(layout=document.model_dump(mode="json"), cross_extractor=document.cross_extractor)
            )
        async with tenant_transaction(engine, tenant_a) as session:
            stored = (await session.execute(select(Upload.layout, Upload.cross_extractor))).one()
        assert LayoutDocument.model_validate(stored.layout, strict=False) == document
        assert stored.cross_extractor == "agree"

    async def test_the_diagnostic_column_takes_two_words_and_nothing_else(
        self, engine: AsyncEngine, tenant_a: TenantId, upload_id: uuid.UUID
    ) -> None:
        with pytest.raises(IntegrityError, match="cross_extractor_is_known"):
            async with tenant_transaction(engine, tenant_a) as session:
                await session.execute(
                    update(Upload).where(Upload.id == upload_id).values(cross_extractor="100%")
                )
