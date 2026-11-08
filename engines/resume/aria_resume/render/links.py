"""Clickable contact links, added to the printed PDF as annotations over the text.

The contact line is printed as one plain text node (RESUME_ENGINE §10): an `<a>` or a
styled span there makes Chromium place every glyph on its own, and extractors read the
email address one letter at a time. So the links are not markup. After printing, each
linked item's characters are found on the page and a Link annotation is laid over them.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import pdfplumber
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Link

from aria_resume.compose.line_budget import contact_rows
from aria_resume.document import CONTACT_SEPARATOR, ResumeDocument
from aria_resume.render.pdf import RenderError

__all__ = ["Box", "add_links", "locate_contact_items"]


@dataclass(frozen=True, slots=True)
class Box:
    """A rectangle in PDF user space: origin bottom-left, as annotations want it."""

    x0: float
    y0: float
    x1: float
    y1: float


def locate_contact_items(pdf: bytes, document: ResumeDocument) -> dict[str, Box]:
    """Where each contact item's text sits on page 1, by the item's text.

    Matched on non-space characters in reading order, since whether a printed space is a
    glyph or a gap varies by renderer. Each contact line is found whole first, so a phone
    number repeated in a bullet cannot be mistaken for the one in the header, and must sit on
    one printed line: an item broken across two would get one link box over both lines and
    the items between them.
    """
    with pdfplumber.open(io.BytesIO(pdf)) as document_pdf:
        page = document_pdf.pages[0]
        height = float(page.height)
        chars = sorted(
            (char for char in page.chars if not char["text"].isspace()),
            key=lambda char: (round(float(char["top"])), float(char["x0"])),
        )
    stream = "".join(char["text"] for char in chars)
    found: dict[str, Box] = {}
    offset = 0
    for row in contact_rows([item.text for item in document.contact]):
        line = "".join(CONTACT_SEPARATOR.join(row).split())
        start = stream.find(line, offset)
        if start < 0:
            raise RenderError(
                f"the contact line {CONTACT_SEPARATOR.join(row)!r} is not on page 1 as one run of text"
            )
        if len({round(float(char["top"])) for char in chars[start : start + len(line)]}) > 1:
            raise RenderError(
                f"the contact line {CONTACT_SEPARATOR.join(row)!r} is broken across printed lines"
            )
        offset = start
        for text in row:
            bare = "".join(text.split())
            at = stream.find(bare, offset, start + len(line))
            if at < 0:
                raise RenderError(f"contact item {text!r} is not where the contact line says")
            span = chars[at : at + len(bare)]
            found[text] = Box(
                x0=min(float(char["x0"]) for char in span),
                y0=height - max(float(char["bottom"]) for char in span),
                x1=max(float(char["x1"]) for char in span),
                y1=height - min(float(char["top"]) for char in span),
            )
            offset = at + len(bare)
        offset = start + len(line)
    return found


def add_links(pdf: bytes, document: ResumeDocument) -> bytes:
    """The PDF with a borderless link over every contact item that has a url."""
    boxes = locate_contact_items(pdf, document)
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf)))
    for item in document.contact:
        if item.url is None:
            continue
        box = boxes[item.text]
        writer.add_annotation(
            page_number=0,
            annotation=Link(rect=(box.x0, box.y0, box.x1, box.y1), url=item.url, border=[0, 0, 0]),
        )
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
