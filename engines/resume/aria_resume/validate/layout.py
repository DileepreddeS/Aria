"""6d Layout check (RESUME_ENGINE §9): one page, fonts embedded, links right, one date format.

Orphan lines need the page read back line by line, which is the ATS check's job (6a); they
are checked there, through Stage 0's own reading of the page.
"""

from __future__ import annotations

import io
import re
from typing import Any

from pypdf import PdfReader

from aria_resume.document import ResumeDocument
from aria_resume.render.links import Box, locate_contact_items
from aria_resume.render.pdf import RenderError
from aria_resume.validate import Finding

__all__ = ["check_layout"]

_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{4}"
#: The one date style a resume uses: two months, or a month and "Present", joined by a
#: spaced en dash (written as an escape the regex engine reads); or a single month, for a
#: degree.
_DATES = re.compile(rf"^{_MONTH}(?: \u2013 (?:{_MONTH}|Present))?$")


def _embedded(font: Any) -> bool:
    font = font.get_object()
    if font.get("/Subtype") == "/Type0":
        font = font["/DescendantFonts"][0].get_object()
    descriptor = font.get("/FontDescriptor")
    if descriptor is None:
        return False
    descriptor = descriptor.get_object()
    return any(key in descriptor for key in ("/FontFile", "/FontFile2", "/FontFile3"))


def _links(reader: PdfReader) -> list[tuple[str, tuple[float, ...]]]:
    links = []
    for annotation in reader.pages[0].get("/Annots") or []:
        annotation = annotation.get_object()
        if annotation.get("/Subtype") == "/Link" and "/A" in annotation:
            uri = annotation["/A"].get_object().get("/URI")
            if uri is not None:
                links.append((str(uri), tuple(float(value) for value in annotation["/Rect"])))
    return links


def _covers(rect: tuple[float, ...], box: Box) -> bool:
    x0, y0, x1, y1 = rect
    middle_x, middle_y = (box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2
    return min(x0, x1) <= middle_x <= max(x0, x1) and min(y0, y1) <= middle_y <= max(y0, y1)


def check_layout(pdf: bytes, document: ResumeDocument) -> list[Finding]:
    reader = PdfReader(io.BytesIO(pdf))
    findings: list[Finding] = []

    if len(reader.pages) != 1:
        findings.append(Finding("one_page", f"the resume runs to {len(reader.pages)} pages"))

    for number, page in enumerate(reader.pages, start=1):
        resources = page.get("/Resources")
        fonts: Any = resources.get_object().get("/Font") if resources is not None else None
        for name, font in fonts.get_object().items() if fonts is not None else ():
            if not _embedded(font):
                findings.append(Finding("fonts_embedded", f"font {name} on page {number} is not embedded"))

    expected = [item for item in document.contact if item.url is not None]
    links = _links(reader)
    try:
        boxes = locate_contact_items(pdf, document)
    except RenderError as error:
        findings.append(Finding("links", str(error)))
        boxes = {}
    for item in expected:
        rects = [rect for uri, rect in links if uri == item.url]
        if not rects:
            findings.append(Finding("links", f"{item.text} has no link to {item.url}"))
        elif item.text in boxes and not any(_covers(rect, boxes[item.text]) for rect in rects):
            findings.append(Finding("links", f"the link to {item.url} is not over {item.text}"))
    stray = {uri for uri, _ in links} - {item.url for item in expected}
    for uri in sorted(stray):
        findings.append(Finding("links", f"a link to {uri} that no contact item asked for"))

    for section in document.sections:
        for entry in section.entries:
            if entry.dates and not _DATES.match(entry.dates):
                findings.append(
                    Finding(
                        "date_format", f"{entry.title}: {entry.dates!r} is not in the resume's date format"
                    )
                )
    return findings
