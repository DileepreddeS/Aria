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

from aria_resume.document import ResumeDocument
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
    glyph or a gap varies by renderer. The whole contact line is found first, so a phone
    number repeated in a bullet cannot be mistaken for the one in the header.
    """
    with pdfplumber.open(io.BytesIO(pdf)) as document_pdf:
        page = document_pdf.pages[0]
        height = float(page.height)
        chars = sorted(
            (char for char in page.chars if not char["text"].isspace()),
            key=lambda char: (round(float(char["top"])), float(char["x0"])),
        )
    stream = "".join(char["text"] for char in chars)
    line = "".join(document.contact_line.split())
    start = stream.find(line)
    if not line or start < 0:
        raise RenderError("the contact line is not on page 1 as one run of text")
    found: dict[str, Box] = {}
    offset = start
    for item in document.contact:
        text = "".join(item.text.split())
        at = stream.find(text, offset, start + len(line))
        if at < 0:
            raise RenderError(f"contact item {item.text!r} is not where the contact line says")
        span = chars[at : at + len(text)]
        found[item.text] = Box(
            x0=min(float(char["x0"]) for char in span),
            y0=height - max(float(char["bottom"]) for char in span),
            x1=max(float(char["x1"]) for char in span),
            y1=height - min(float(char["top"]) for char in span),
        )
        offset = at + len(text)
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
