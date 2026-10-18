"""Stage 7, the PDF half, and the 6d layout check.

The text layer is the product: what two independent extractors read back is what an ATS
reads. The printing tests need Playwright's Chromium (`uv run playwright install chromium`); in CI a
missing browser fails them rather than skipping them.
"""

from __future__ import annotations

import asyncio
import io
from functools import cache
from typing import Any

import pdfplumber
import pytest
from aria_resume.document import (
    ContactItem,
    DocumentBullet,
    DocumentEntry,
    DocumentSection,
    ResumeDocument,
    SectionKind,
)
from aria_resume.render.html import render_html
from aria_resume.render.links import add_links
from aria_resume.render.pdf import ChromiumPrinter, RenderError
from aria_resume.validate.layout import check_layout
from pypdf import PdfReader, PdfWriter

EN = "\u2013"
EMAIL = "jordan.rivera@example.com"
LINKEDIN = "linkedin.com/in/jordan-rivera-example"


def _document(*, bullets: int = 2, dates: str = f"Feb 2024 {EN} Jul 2025") -> ResumeDocument:
    texts = [
        # "efficient", "office", "workflow": words Carlito would set with fi/ffi/fl ligatures.
        "Cut p95 API latency by 30% with Redis caching, an efficient fix the office adopted",
        "Built 15+ React screens and a workflow for the checkout platform",
    ]
    return ResumeDocument(
        name="Jordan A. Rivera",
        contact=(
            ContactItem(text=EMAIL, url=f"mailto:{EMAIL}"),
            ContactItem(text="(555) 014-2288"),
            ContactItem(text=LINKEDIN, url=f"https://{LINKEDIN}"),
        ),
        sections=(
            DocumentSection(
                kind=SectionKind.EXPERIENCE,
                entries=(
                    DocumentEntry(
                        title="Nordvale Consulting",
                        detail="Software Engineer",
                        dates=dates,
                        bullets=tuple(
                            DocumentBullet(text=texts[index % 2] + (f" ({index})" if index > 1 else ""))
                            for index in range(bullets)
                        ),
                    ),
                ),
            ),
        ),
    )


def _print(document: ResumeDocument) -> bytes:
    return add_links(asyncio.run(ChromiumPrinter().print_pdf(render_html(document))), document)


@cache
def _printed() -> bytes:
    return _print(_document())


def _pypdf_text(pdf: bytes) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)


def _pdfplumber_text(pdf: bytes) -> str:
    with pdfplumber.open(io.BytesIO(pdf)) as document:
        return "\n".join(page.extract_text() or "" for page in document.pages)


@pytest.mark.chromium
class TestThePrintedTextLayer:
    @pytest.mark.parametrize("extract", [_pypdf_text, _pdfplumber_text], ids=["pypdf", "pdfplumber"])
    def test_the_contact_line_reads_whole(self, extract: object) -> None:
        assert callable(extract)
        assert f"{EMAIL} | (555) 014-2288 | {LINKEDIN}" in extract(_printed())

    @pytest.mark.parametrize("extract", [_pypdf_text, _pdfplumber_text], ids=["pypdf", "pdfplumber"])
    def test_no_ligature_or_unmapped_glyph(self, extract: object) -> None:
        assert callable(extract)
        text = extract(_printed())
        assert "efficient" in text and "office" in text and "workflow" in text
        assert not any("\ufb00" <= character <= "\ufb06" for character in text)
        assert "(cid:" not in text

    def test_each_bullet_keeps_its_bullet_character_and_follows_its_header(self) -> None:
        lines = [line.strip() for line in _pdfplumber_text(_printed()).splitlines()]
        header = next(index for index, line in enumerate(lines) if line.startswith("Nordvale Consulting"))
        assert lines[header].endswith(f"Feb 2024 {EN} Jul 2025")
        assert lines[header + 1].startswith("\u2022") and lines[header + 2].startswith("\u2022")

    def test_the_layout_check_passes(self) -> None:
        assert check_layout(_printed(), _document()) == []

    def test_the_links_are_annotations_with_the_right_targets(self) -> None:
        listed: Any = PdfReader(io.BytesIO(_printed())).pages[0]["/Annots"]
        annotations = [annotation.get_object() for annotation in listed]
        assert sorted(str(annotation["/A"]["/URI"]) for annotation in annotations) == [
            f"https://{LINKEDIN}",
            f"mailto:{EMAIL}",
        ]


@pytest.mark.chromium
class TestThePageCannotReachOut:
    def test_a_request_for_anything_is_refused_and_is_an_error(self) -> None:
        html = render_html(_document()).replace("<body>", '<body><img src="https://example.com/pixel.png">')
        with pytest.raises(RenderError, match="asked the network"):
            asyncio.run(ChromiumPrinter().print_pdf(html))

    def test_javascript_does_not_run(self) -> None:
        html = render_html(_document()).replace(
            "</body>", "<script>document.body.innerText = 'REWRITTEN BY SCRIPT';</script></body>"
        )
        pdf = asyncio.run(ChromiumPrinter().print_pdf(html))
        assert "REWRITTEN" not in _pypdf_text(pdf)
        assert EMAIL in _pypdf_text(pdf)


@pytest.mark.chromium
class TestTheLayoutCheckFindsProblems:
    def test_a_second_page(self) -> None:
        document = _document(bullets=70)
        findings = check_layout(_print(document), document)
        assert [finding.check for finding in findings] == ["one_page"]

    def test_the_bundled_font_is_the_one_printed(self) -> None:
        assert not [f for f in check_layout(_printed(), _document()) if f.check == "font_substituted"]

    def test_a_page_set_in_a_stand_in_font_is_refused(self) -> None:
        # If Carlito fails to load, Chromium prints in a system font (Arial on Windows),
        # wider than the line budget measured, and still embeds it.
        import re

        document = _document()
        html = re.sub(r"@font-face \{[^}]*\}", "", render_html(document))
        findings = check_layout(add_links(asyncio.run(ChromiumPrinter().print_pdf(html)), document), document)
        assert "font_substituted" in {finding.check for finding in findings}
        assert "fonts_embedded" not in {finding.check for finding in findings}

    def test_a_missing_link(self) -> None:
        writer = PdfWriter(clone_from=PdfReader(io.BytesIO(_printed())))
        del writer.pages[0]["/Annots"]
        out = io.BytesIO()
        writer.write(out)
        findings = check_layout(out.getvalue(), _document())
        assert sorted(finding.detail for finding in findings) == [
            f"{EMAIL} has no link to mailto:{EMAIL}",
            f"{LINKEDIN} has no link to https://{LINKEDIN}",
        ]


class TestTheLayoutCheckWithoutABrowser:
    def test_a_date_in_another_format(self) -> None:
        # The PDF is not read for dates: they are checked on the document.
        findings = check_layout(_minimal_pdf(), _document(dates="Feb 2024 - Jul 2025"))
        assert any(finding.check == "date_format" for finding in findings)

    def test_a_font_that_is_not_embedded(self) -> None:
        findings = check_layout(_minimal_pdf(), _document())
        assert any(finding.check == "fonts_embedded" for finding in findings)


def _minimal_pdf() -> bytes:
    """One page set in Helvetica, which a PDF may name without embedding."""
    stream = b"BT /F1 11 Tf 72 720 Td (Jordan A. Rivera) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
