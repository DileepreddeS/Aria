"""PDFs that RESUME_ENGINE §5.1 must refuse, built from code.

Each is the smallest PDF that has the property: a scan, a font whose glyphs cannot be
mapped back to letters, a password, a permission bit. Built here rather than committed,
so a reader can see exactly what is being refused and why.
"""

from __future__ import annotations

import io

import pypdf
from fixtures import _assemble, _escape_pdf_string, _stream_object, make_pdf  # type: ignore[import-not-found]
from pypdf.constants import UserAccessPermissions

__all__ = ["cid_font_pdf", "encrypted_but_open_pdf", "encrypted_pdf", "permission_locked_pdf", "scanned_pdf"]


def scanned_pdf() -> bytes:
    """One page that is one image and no text: what a scanner produces."""
    image = (
        b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray "
        b"/BitsPerComponent 8 /Length 1 >>\nstream\n\xf0\nendstream"
    )
    pdf: bytes = _assemble(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /XObject << /Im1 4 0 R >> >> /Contents 5 0 R >>",
            image,
            _stream_object(b"q 612 0 0 792 0 0 cm /Im1 Do Q"),
        ]
    )
    return pdf


#: Maps CIDs 0x20-0x7E to the same Unicode code points: a working ToUnicode table.
_TO_UNICODE = (
    b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap "
    b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def "
    b"/CMapName /Adobe-Identity-UCS def /CMapType 2 def "
    b"1 begincodespacerange <0000> <FFFF> endcodespacerange "
    b"1 beginbfrange <0020> <007E> <0020> endbfrange "
    b"endcmap CMapName currentdict /CMap defineresource pop end end"
)


def cid_font_pdf(*, readable: str, encoded: str, mapped: bool = False) -> bytes:
    """``readable`` in Helvetica, then ``encoded`` in a two-byte CID font.

    Without ``mapped`` the CID font has no ToUnicode table, so its characters come out
    as ``(cid:N)``: the text is drawn on the page and cannot be read back from it.
    ``mapped`` adds the table, the control case.
    """
    cids = "".join(f"{ord(character):04X}" for character in encoded).encode()
    content = (
        b"BT /F1 10 Tf 72 740 Td (" + _escape_pdf_string(readable) + b") Tj ET "
        b"BT /F2 10 Tf 72 700 Td <" + cids + b"> Tj ET"
    )
    type0 = (
        b"<< /Type /Font /Subtype /Type0 /BaseFont /ABCDEF+Mystery /Encoding /Identity-H "
        b"/DescendantFonts [7 0 R]" + (b" /ToUnicode 9 0 R" if mapped else b"") + b" >>"
    )
    pdf: bytes = _assemble(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            type0,
            _stream_object(content),
            b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /ABCDEF+Mystery "
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
            b"/FontDescriptor 8 0 R /DW 500 >>",
            b"<< /Type /FontDescriptor /FontName /ABCDEF+Mystery /Flags 32 /FontBBox [0 -200 1000 800] "
            b"/ItalicAngle 0 /Ascent 800 /Descent -200 /CapHeight 700 /StemV 80 >>",
            _stream_object(_TO_UNICODE),
        ]
    )
    return pdf


def _encrypt(user_password: str, permissions: UserAccessPermissions) -> bytes:
    reader = pypdf.PdfReader(io.BytesIO(make_pdf()))
    writer = pypdf.PdfWriter(clone_from=reader)
    writer.encrypt(user_password=user_password, owner_password="owner", permissions_flag=permissions)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def encrypted_pdf() -> bytes:
    """A PDF that needs a password to open at all."""
    return _encrypt("a-password", UserAccessPermissions.all())


def permission_locked_pdf() -> bytes:
    """Opens without a password, but its permissions forbid extracting text."""
    return _encrypt("", UserAccessPermissions.PRINT)


def encrypted_but_open_pdf() -> bytes:
    """Encrypted with an empty password and every permission: readable."""
    return _encrypt("", UserAccessPermissions.all())
