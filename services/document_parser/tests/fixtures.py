"""Documents built from code, so no binary fixture is committed.

A committed .pdf or .docx is a file nobody reviews and everybody trusts. Building them
here means a reader can see exactly what is being parsed — including the hostile ones,
where the interesting part *is* the construction.
"""

from __future__ import annotations

import io
import zipfile

__all__ = [
    "RESUME_LINES",
    "docx_document_xml",
    "image_only_pdf",
    "make_docx",
    "make_pdf",
    "make_pdf_with_links",
    "multi_page_pdf",
    "three_column_pdf",
    "two_column_pdf",
]

RESUME_LINES = [
    "Jordan A. Rivera",
    "Software Engineer | jordan.rivera@example.com | github.com/example",
    "",
    "EXPERIENCE",
    "Accenture (client: Carvana) - Software Engineer",
    "Jun 2024 - Present",
    "Built 20+ React components used by 4 teams",
    "Cut p95 API latency by 30% by adding Redis caching",
    "",
    "EDUCATION",
    "BS Computer Science, Example University, 2024",
]

_FILLER = "Resume text with enough characters to clear the text-layer floor. " * 6


def _assemble(objects: list[bytes]) -> bytes:
    """Wrap PDF objects in a header, cross-reference table and trailer."""
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n").encode()
    return bytes(out)


def _escape_pdf_string(value: str) -> bytes:
    """Escape a PDF literal string: backslash and both parentheses."""
    escaped = value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return escaped.encode("latin-1", "replace")


def _text_stream(text: str) -> bytes:
    return b"BT /F1 12 Tf 72 720 Td (" + _escape_pdf_string(text) + b") Tj ET"


def _font_object() -> bytes:
    return b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"


def _stream_object(stream: bytes) -> bytes:
    return b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"


def make_pdf(text: str = _FILLER) -> bytes:
    """A valid single-page PDF with a real text layer.

    Hand-built rather than produced by a writer library, so the tests depend on the
    two readers under test and nothing else.
    """
    return _assemble(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            _font_object(),
            _stream_object(_text_stream(text)),
        ]
    )


def make_pdf_with_links(uris: list[str], text: str = _FILLER) -> bytes:
    """A single-page PDF whose page carries link annotations.

    A real resume puts its LinkedIn and GitHub URLs in annotations rather than in the
    text layer, which is why extracting the text alone loses them.
    """
    # Objects 1-5 are the catalog, page tree, page, font and content stream, so the
    # annotations start at 6.
    annotation_ids = [6 + index for index in range(len(uris))]
    annots = " ".join(f"{identifier} 0 R" for identifier in annotation_ids)

    page = (
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R"
        + (f" /Annots [{annots}]".encode() if uris else b"")
        + b" >>"
    )

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        page,
        _font_object(),
        _stream_object(_text_stream(text)),
    ]
    objects.extend(
        b"<< /Type /Annot /Subtype /Link /Rect [72 700 300 720] "
        b"/A << /Type /Action /S /URI /URI (" + _escape_pdf_string(uri) + b") >> >>"
        for uri in uris
    )
    return _assemble(objects)


def multi_page_pdf(pages: int) -> bytes:
    """A PDF claiming ``pages`` pages, for the page-cap test."""
    # Objects 1-4 are the catalog, page tree, font and shared content stream; pages
    # start at 5.
    page_ids = [5 + index for index in range(pages)]
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode(),
        _font_object(),
        _stream_object(_text_stream(_FILLER)),
    ]
    objects.extend(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 3 0 R >> >> /Contents 4 0 R >>"
        for _ in range(pages)
    )
    return _assemble(objects)


def image_only_pdf() -> bytes:
    """A valid PDF with no text layer: what a scan looks like."""
    return _assemble(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>",
            b"<< /Length 0 >>\nstream\n\nendstream",
        ]
    )


#: The content type aria_api.uploads.detect looks for, so a fixture that should be
#: recognised as a DOCX really is. Acceptance is a separate question: the allowlist
#: declines .docx by default.
DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
WORD_NAMESPACE = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def docx_document_xml(paragraphs: list[str]) -> str:
    body = "".join(f"<w:p><w:r><w:t>{paragraph}</w:t></w:r></w:p>" for paragraph in paragraphs)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<w:document {WORD_NAMESPACE}><w:body>{body}</w:body></w:document>"
    )


def make_docx(
    paragraphs: list[str] | None = None,
    *,
    document_xml: str | None = None,
    extra_parts: dict[str, str] | None = None,
    content_type: str = DOCX_CONTENT_TYPE,
    compress: bool = True,
) -> bytes:
    """A .docx with a real manifest. ``document_xml`` overrides the body wholesale."""
    lines = paragraphs if paragraphs is not None else RESUME_LINES
    buffer = io.BytesIO()
    mode = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(buffer, "w", mode) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types><Override PartName="/word/document.xml" '
            f'ContentType="{content_type}"/></Types>',
        )
        archive.writestr("word/document.xml", document_xml or docx_document_xml(lines))
        for name, body in (extra_parts or {}).items():
            archive.writestr(name, body)
    return buffer.getvalue()


def _positioned_lines(lines: list[str], *, x: int, top: int, leading: int) -> bytes:
    """Text operators placing each line at an absolute position.

    Absolute ``Tm`` per line rather than ``T*`` relative movement, because that is
    how a writer emits a multi-column page: it jumps between columns a visual row at
    a time rather than finishing one column first.
    """
    parts: list[bytes] = []
    for index, line in enumerate(lines):
        parts.append(f" 1 0 0 1 {x} {top - index * leading} Tm".encode())
        parts.append(b" (" + _escape_pdf_string(line) + b") Tj")
    return b"".join(parts)


def two_column_pdf(
    left: list[str],
    right: list[str],
    *,
    left_x: int = 50,
    right_x: int = 330,
    top: int = 720,
    leading: int = 18,
    banner: tuple[str, int] | None = None,
    header: str | None = None,
) -> bytes:
    """A page with two columns, emitted **interleaved** a visual row at a time.

    This is what a real two-column resume looks like inside the file, and it is the
    case both extractors get wrong in the same way: each flattens a visual row into
    one line, so they agree with each other and neither is right.

    ``header`` spans both columns above them. ``banner`` spans both columns at the y
    given, which is the layout the geometry cannot order.
    """
    parts = [b"BT /F1 11 Tf"]
    if header is not None:
        parts.append(f" 1 0 0 1 {left_x} {top + 2 * leading} Tm".encode())
        parts.append(b" (" + _escape_pdf_string(header) + b") Tj")
    if banner is not None:
        banner_text, banner_y = banner
        parts.append(f" 1 0 0 1 {left_x} {banner_y} Tm".encode())
        parts.append(b" (" + _escape_pdf_string(banner_text) + b") Tj")

    for index in range(max(len(left), len(right))):
        row_y = top - index * leading
        for x, column in ((left_x, left), (right_x, right)):
            if index < len(column):
                parts.append(f" 1 0 0 1 {x} {row_y} Tm".encode())
                parts.append(b" (" + _escape_pdf_string(column[index]) + b") Tj")
    parts.append(b" ET")

    return _assemble(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            _font_object(),
            _stream_object(b"".join(parts)),
        ]
    )


def three_column_pdf(first: list[str], second: list[str], third: list[str]) -> bytes:
    """Three columns, which the geometry can see and cannot order."""
    parts = [b"BT /F1 9 Tf"]
    for index in range(max(len(first), len(second), len(third))):
        row_y = 720 - index * 18
        for x, column in ((40, first), (240, second), (440, third)):
            if index < len(column):
                parts.append(f" 1 0 0 1 {x} {row_y} Tm".encode())
                parts.append(b" (" + _escape_pdf_string(column[index]) + b") Tj")
    parts.append(b" ET")
    return _assemble(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            _font_object(),
            _stream_object(b"".join(parts)),
        ]
    )
