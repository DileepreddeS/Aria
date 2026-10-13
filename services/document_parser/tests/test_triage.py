"""Glyphs and triage: what Stage 0 reads from, and whether it may read at all (§5.1)."""

from __future__ import annotations

import io
from pathlib import Path

import pdfplumber
import pypdf
import pytest
from fixtures import image_only_pdf, make_pdf  # type: ignore[import-not-found]
from pathology_pdfs import (  # type: ignore[import-not-found]
    cid_font_pdf,
    encrypted_but_open_pdf,
    encrypted_pdf,
    permission_locked_pdf,
    scanned_pdf,
)

from aria_core.documents import Triage
from aria_document_parser.glyphs import Glyph, base_font_name, page_glyphs, weight_from_name
from aria_document_parser.triage import (
    MAX_UNREADABLE_SHARE,
    MIN_CHARACTERS_PER_PAGE,
    PageCensus,
    census,
    decide,
    lock_reason,
)

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "resumes"
RESUME_BODY = "Software Engineer at Nordvale Consulting, built 15+ React screens. " * 20


def _triage(content: bytes) -> tuple[Triage, str, list[PageCensus]]:
    """The order pdf.py will use: the lock first, because a locked file cannot be opened."""
    locked = lock_reason(pypdf.PdfReader(io.BytesIO(content)))
    if locked is not None:
        verdict, detail = decide([], locked)
        return verdict, detail, []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        pages = census(content, pdf.pages, [page_glyphs(page) for page in pdf.pages])
    verdict, detail = decide(pages)
    return verdict, detail, pages


class TestWeightFromTheFontName:
    @pytest.mark.parametrize(
        "name",
        [
            "Carlito-Bold",
            "Arial-BoldMT",
            "Calibri,Bold",
            "Montserrat-SemiBold",
            "SourceSansPro-Semibold",
            "OpenSans-ExtraBold",
            "Lato-Black",
            "TimesNewRomanPS-BoldItalicMT",
            "ArialBold",
            "CMBX10",
            "SFBX1200",
        ],
    )
    def test_bold_names(self, name: str) -> None:
        assert weight_from_name(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "Carlito-Regular",
            "Carlito-Italic",
            "Inter-Medium",
            "Roboto-Light",
            "Georgia,Italic",
            "CMR10",
            "SFRM1000",
        ],
    )
    def test_named_weights_that_are_not_bold(self, name: str) -> None:
        assert weight_from_name(name) is False

    @pytest.mark.parametrize("name", ["Helvetica", "Carlito", "Mystery"])
    def test_a_name_without_a_weight_says_nothing_rather_than_regular(self, name: str) -> None:
        # Unknown is not regular: line building measures ink when names are silent.
        assert weight_from_name(name) is None

    def test_a_family_containing_bold_letters_is_judged_by_its_style(self) -> None:
        assert weight_from_name("Blackwood-Regular") is False

    def test_the_subset_prefix_is_removed_before_matching(self) -> None:
        assert base_font_name("AAAAAA+Carlito-Bold") == "Carlito-Bold"
        assert weight_from_name("AAAAAA+Carlito-Bold") is True
        assert base_font_name("Carlito-Bold") == "Carlito-Bold"


class TestGlyphs:
    def test_every_character_arrives_with_its_box_and_font(self) -> None:
        with pdfplumber.open(io.BytesIO(make_pdf("Jordan Rivera"))) as pdf:
            glyphs = page_glyphs(pdf.pages[0])
        assert "".join(glyph.text for glyph in glyphs) == "Jordan Rivera"
        first = glyphs[0]
        assert first.font == "Helvetica" and first.bold is None and first.upright
        assert first.x1 > first.x0 and first.bottom > first.top and first.size == 12

    def test_a_real_resume_names_its_bold(self) -> None:
        with pdfplumber.open(FIXTURES / "clean_inline_block.pdf") as pdf:
            glyphs = page_glyphs(pdf.pages[0])
        assert {glyph.bold for glyph in glyphs} == {True, False}

    @pytest.mark.parametrize(("text", "unreadable"), [("(cid:65)", True), ("\ufffd", True), ("a", False)])
    def test_an_unmappable_character_is_recognised(self, text: str, unreadable: bool) -> None:
        glyph = Glyph(text, 0.0, 1.0, 0.0, 1.0, 10.0, "F", None, True)
        assert glyph.unreadable is unreadable


class TestEachRowOfTheTriageTable:
    def test_a_password_protected_pdf_is_locked(self) -> None:
        verdict, detail, _ = _triage(encrypted_pdf())
        assert verdict is Triage.LOCKED
        assert "password" in detail

    def test_a_pdf_whose_permissions_forbid_extraction_is_locked(self) -> None:
        verdict, detail, _ = _triage(permission_locked_pdf())
        assert verdict is Triage.LOCKED
        assert "permissions" in detail

    def test_encryption_alone_is_not_a_lock(self) -> None:
        assert _triage(encrypted_but_open_pdf())[0] is Triage.READABLE

    def test_a_scan_has_no_text_layer(self) -> None:
        verdict, detail, pages = _triage(scanned_pdf())
        assert verdict is Triage.NO_TEXT_LAYER
        assert pages[0].full_page_image
        assert "scanned" in detail

    def test_a_blank_page_has_no_text_layer_either(self) -> None:
        verdict, detail, _ = _triage(image_only_pdf())
        assert verdict is Triage.NO_TEXT_LAYER
        assert "scanned" not in detail

    def test_a_page_of_almost_nothing_is_mostly_image(self) -> None:
        assert _triage(make_pdf("Jordan A. Rivera"))[0] is Triage.MOSTLY_IMAGE

    def test_a_font_without_a_unicode_map_is_unreadable(self) -> None:
        verdict, _, pages = _triage(cid_font_pdf(readable="Jordan A. Rivera", encoded=RESUME_BODY))
        assert verdict is Triage.UNREADABLE_FONT
        assert {font.name: font.has_to_unicode for font in pages[0].fonts}["Mystery"] is False

    def test_the_same_font_with_its_map_reads_fine(self) -> None:
        verdict, _, pages = _triage(
            cid_font_pdf(readable="Jordan A. Rivera", encoded=RESUME_BODY, mapped=True)
        )
        assert verdict is Triage.READABLE
        assert {font.name: font.has_to_unicode for font in pages[0].fonts}["Mystery"] is True

    @pytest.mark.parametrize(
        "fixture",
        [
            "clean_inline_block",
            "float_scrambled",
            "markerless_bullets",
            "no_marker_bullets",
            "per_glyph_contact",
        ],
    )
    def test_every_gate_fixture_is_readable(self, fixture: str) -> None:
        assert _triage((FIXTURES / f"{fixture}.pdf").read_bytes())[0] is Triage.READABLE


class TestTheThresholds:
    @staticmethod
    def _page(characters: int, unreadable: int = 0) -> PageCensus:
        return PageCensus(1, 612.0, 792.0, characters, unreadable, full_page_image=False)

    def test_two_hundred_characters_a_page_is_enough(self) -> None:
        assert decide([self._page(MIN_CHARACTERS_PER_PAGE)])[0] is Triage.READABLE
        assert decide([self._page(MIN_CHARACTERS_PER_PAGE - 1)])[0] is Triage.MOSTLY_IMAGE

    def test_the_average_counts_across_pages(self) -> None:
        # A dense first page does not excuse an image-only second.
        pages = [self._page(300), PageCensus(2, 612.0, 792.0, 0, 0, full_page_image=True)]
        assert decide(pages)[0] is Triage.MOSTLY_IMAGE

    def test_a_quarter_unreadable_is_tolerated_and_more_is_not(self) -> None:
        assert decide([self._page(1000, int(1000 * MAX_UNREADABLE_SHARE))])[0] is Triage.READABLE
        assert decide([self._page(1000, int(1000 * MAX_UNREADABLE_SHARE) + 1)])[0] is Triage.UNREADABLE_FONT

    def test_every_refusal_says_what_else_the_candidate_can_do(self) -> None:
        refusals = [
            decide([], "it is protected by a password"),
            decide([self._page(0)]),
            decide([self._page(10)]),
            decide([self._page(1000, 900)]),
        ]
        for verdict, detail in refusals:
            assert verdict is not Triage.READABLE
            assert "paste the text" in detail
