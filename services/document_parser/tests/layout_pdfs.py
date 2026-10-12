"""Single-page PDFs built from drawing operators, for testing line building.

Each builder produces the smallest page that has one property — a date at the right
margin, a bullet drawn as a shape, text placed one character at a time — so a failing
test points at one rule.
"""

from __future__ import annotations

import io

import pdfplumber
from fixtures import _assemble, _stream_object  # type: ignore[import-not-found]

__all__ = ["page_pdf", "per_glyph_text", "text_at"]

#: Font resource names to base fonts. F3 deliberately has no weight in its name.
FONTS = {"F1": "Helvetica", "F2": "Helvetica-Bold", "F3": "Helvetica"}


def page_pdf(content: bytes, fonts: dict[str, str] | None = None) -> bytes:
    """One 612 x 792 page drawing ``content`` with the given fonts."""
    resources = fonts or FONTS
    font_ids = {name: 5 + index for index, name in enumerate(resources)}
    font_dict = " ".join(f"/{name} {font_ids[name]} 0 R" for name in resources).encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << "
        + font_dict
        + b" >> >> /Contents 4 0 R >>",
        _stream_object(content),
    ]
    # WinAnsiEncoding, so the bullet and the dashes a resume uses are drawable.
    objects.extend(
        f"<< /Type /Font /Subtype /Type1 /BaseFont /{base} /Encoding /WinAnsiEncoding >>".encode()
        for base in resources.values()
    )
    pdf: bytes = _assemble(objects)
    return pdf


def _literal(text: str) -> bytes:
    """A PDF string literal in WinAnsi (cp1252), with backslash and parentheses escaped."""
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return escaped.encode("cp1252")


def text_at(
    x: float, y: float, text: str, *, font: str = "F1", size: float = 10, state: bytes = b""
) -> bytes:
    """Draw ``text`` with its baseline at (x, y) in PDF coordinates (origin bottom-left)."""
    return (
        b"BT /" + font.encode() + b" " + str(size).encode() + b" Tf " + state + b" "
        + f"{x} {y} Td (".encode() + _literal(text) + b") Tj ET "
    )  # fmt: skip


def per_glyph_text(x: float, y: float, text: str, *, size: float = 10) -> bytes:
    """``text`` drawn one character at a time, each with its own positioning operator.

    What Chromium does for a whole document. Spaces are not drawn at all — only the gap
    they leave — so words exist only in the geometry. Positions come from laying the
    text out normally first, so the gaps are the real ones.
    """
    with pdfplumber.open(io.BytesIO(page_pdf(text_at(x, y, text, size=size)))) as pdf:
        chars = pdf.pages[0].chars
    out = b""
    for char in chars:
        if char["text"].isspace():
            continue
        out += (
            b"BT /F1 " + str(size).encode() + b" Tf "
            + f"1 0 0 1 {char['x0']:.3f} {y} Tm (".encode() + _literal(char["text"]) + b") Tj ET "
        )  # fmt: skip
    return out
