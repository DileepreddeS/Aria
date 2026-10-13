"""Upload to extracted text, through the real parser (SECURITY.md §7).

The unit tests cover each gate; this covers the order they run in, and the state the
``uploads`` row is left in afterwards. Every case here is one a candidate could
actually produce: a resume, a scan, a notes file, the same file twice.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from pathlib import Path
from typing import ClassVar

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_api.uploads.pipeline import UploadResult, find_duplicate_upload, ingest_upload
from aria_api.uploads.quarantine import QuarantineStore
from aria_api.uploads.scanner import DevScanner, ScanStatus
from aria_core.audit.log import read_chain
from aria_core.db.models import Candidate, Upload
from aria_core.db.session import tenant_transaction
from aria_core.documents import LayoutDocument
from aria_core.schemas.identity import TenantId, UserId, new_id
from aria_document_parser.client import InProcessDocumentParser

pytestmark = pytest.mark.db

RESUME_LINES = [
    "Jordan A. Rivera",
    "jordan.rivera@example.com",
    "EXPERIENCE",
    "Accenture - Software Engineer, Jun 2024 - Present",
    "Built 20+ React components used by 4 teams",
]


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


def _two_column_pdf(left: list[str], right: list[str]) -> bytes:
    """Two columns, emitted **interleaved** a visual row at a time.

    What a real two-column resume looks like inside the file, and the case both
    extractors get wrong identically: each flattens a visual row into one line, so
    they agree with each other and neither is right.
    """
    parts = [b"BT /F1 11 Tf"]
    for index in range(max(len(left), len(right))):
        row_y = 720 - index * 18
        for x, column in ((50, left), (330, right)):
            if index < len(column):
                escaped = column[index].replace("(", r"\(").replace(")", r"\)")
                parts.append(f" 1 0 0 1 {x} {row_y} Tm".encode())
                parts.append(b" (" + escaped.encode("latin-1", "replace") + b") Tj")
    parts.append(b" ET")
    stream = b"".join(parts)

    return _assemble(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        ]
    )


def _pdf(text: str) -> bytes:
    """A single-page PDF with a text layer, built by hand (see the parser fixtures)."""
    escaped = text.replace("(", r"\(").replace(")", r"\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1", "replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
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


def _scanned_pdf() -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>",
        b"<< /Length 0 >>\nstream\n\nendstream",
    ]
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


def _pdf_with_links(text: str, uris: list[str]) -> bytes:
    """A PDF whose page carries link annotations, as a real resume's header does."""
    escaped_text = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped_text}) Tj ET".encode("latin-1", "replace")
    annotation_ids = [6 + index for index in range(len(uris))]
    annots = " ".join(f"{identifier} 0 R" for identifier in annotation_ids)

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R"
        + (f" /Annots [{annots}]".encode() if uris else b"")
        + b" >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    for uri in uris:
        objects.append(
            b"<< /Type /Annot /Subtype /Link /Rect [72 700 300 720] "
            b"/A << /Type /Action /S /URI /URI (" + uri.encode("latin-1", "replace") + b") >> >>"
        )

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


def _docx(paragraphs: list[str]) -> bytes:
    namespace = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in paragraphs)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types><Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'wordprocessingml.document"/></Types>',
        )
        archive.writestr(
            "word/document.xml",
            f'<?xml version="1.0"?><w:document {namespace}><w:body>{body}</w:body></w:document>',
        )
    return buffer.getvalue()


@pytest.fixture
def store(tmp_path: Path) -> QuarantineStore:
    return QuarantineStore(tmp_path / "quarantine", max_bytes=10 * 1024 * 1024)


@pytest.fixture
async def candidate_id(engine: AsyncEngine, tenant_a: TenantId, user_a: UserId) -> uuid.UUID:
    created = new_id()
    async with tenant_transaction(engine, tenant_a) as session:
        await session.execute(insert(Candidate).values(id=created, tenant_id=tenant_a, user_id=user_a))
    return created


async def _ingest(
    engine: AsyncEngine,
    tenant_a: TenantId,
    candidate_id: uuid.UUID,
    store: QuarantineStore,
    content: bytes,
    *,
    filename: str = "resume.pdf",
    keep_original: bool = False,
    allow_docx: bool = False,
) -> UploadResult:
    stored = await store.store_bytes(content)
    async with tenant_transaction(engine, tenant_a) as session:
        return await ingest_upload(
            session,
            tenant_id=tenant_a,
            candidate_id=candidate_id,
            original_filename=filename,
            stored=stored,
            store=store,
            scanner=DevScanner(),
            parser=InProcessDocumentParser(),
            keep_original=keep_original,
            allow_docx=allow_docx,
        )


class TestAResumeGoesAllTheWayThrough:
    async def test_a_pdf_becomes_extracted_text(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        store: QuarantineStore,
    ) -> None:
        # Repeated so the text layer clears the 200-character floor that separates a
        # real resume from a scan. A one-page resume is several thousand characters.
        body = " ".join(RESUME_LINES * 4)
        result = await _ingest(engine, tenant_a, candidate_id, store, _pdf(body))

        assert result.accepted
        assert result.detected_type is not None and result.detected_type.value == "pdf"
        assert result.parsed is not None and "React components" in result.parsed.text

        async with tenant_transaction(engine, tenant_a) as session:
            row = (await session.execute(select(Upload))).scalars().one()

        assert row.parse_status == "parsed"
        assert row.scan_status == ScanStatus.NOT_SCANNED.value
        assert "not scanned" in row.scan_detail
        assert row.extracted_text is not None
        assert "React components" in row.extracted_text

    async def test_a_docx_becomes_extracted_text(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        store: QuarantineStore,
    ) -> None:
        result = await _ingest(
            engine,
            tenant_a,
            candidate_id,
            store,
            _docx(RESUME_LINES),
            filename="resume.docx",
            allow_docx=True,
        )
        assert result.accepted
        assert result.detected_type is not None and result.detected_type.value == "docx"
        assert result.parsed is not None and "Accenture" in result.parsed.text

    async def test_a_skills_notes_file_becomes_extracted_text(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        store: QuarantineStore,
    ) -> None:
        # The founder's v1 skills notes arrive as .txt.
        notes = b"Frontend: React, Next.js, TypeScript\nBackend: FastAPI, PostgreSQL\n"
        result = await _ingest(engine, tenant_a, candidate_id, store, notes, filename="skills.txt")
        assert result.accepted
        assert result.detected_type is not None and result.detected_type.value == "txt"
        assert result.parsed is not None and "Next.js" in result.parsed.text

    async def test_the_derivative_is_untrusted_content(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        store: QuarantineStore,
    ) -> None:
        # Text out of somebody else's file is data, never an instruction, and the
        # carrier enforces that (SECURITY.md §5).
        result = await _ingest(engine, tenant_a, candidate_id, store, _docx(RESUME_LINES), allow_docx=True)
        extracted = result.extracted
        assert extracted is not None
        assert extracted.source_kind.value == "uploaded_document"
        assert f"upload:{result.upload_id}" == extracted.source_ref
        # Interpolating it yields a marker rather than the resume.
        assert "Accenture" not in f"{extracted}"


class TestDocxIsDeclinedEndToEnd:
    async def test_a_word_document_upload_is_refused_and_audited(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        store: QuarantineStore,
    ) -> None:
        result = await _ingest(
            engine, tenant_a, candidate_id, store, _docx(RESUME_LINES), filename="resume.docx"
        )

        assert result.refusal is not None
        assert result.refusal.reason == "unsupported_type"
        assert "Save As" in result.refusal.advice

        async with tenant_transaction(engine, tenant_a) as session:
            rows = (await session.execute(select(Upload))).scalars().all()
            events = await read_chain(session, tenant_a)

        assert rows == []
        assert events[0].action == "upload.refused"
        assert list(store.root.iterdir()) == []


class TestLinksAndReview:
    async def test_pdf_links_reach_the_extracted_text(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        store: QuarantineStore,
    ) -> None:
        # The candidate's GitHub and LinkedIn URLs live in annotations, so they would
        # be lost by extracting the text layer alone.
        content = _pdf_with_links(
            " ".join(RESUME_LINES * 4),
            ["https://github.com/example", "https://linkedin.com/in/example"],
        )
        result = await _ingest(engine, tenant_a, candidate_id, store, content)

        assert result.parsed is not None
        assert "github.com/example" in result.parsed.text
        assert "https://linkedin.com/in/example" in result.parsed.links

        async with tenant_transaction(engine, tenant_a) as session:
            row = (await session.execute(select(Upload))).scalars().one()
        assert row.extracted_text is not None
        assert "linkedin.com/in/example" in row.extracted_text

    async def test_the_chosen_extractor_is_recorded_on_the_upload(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        store: QuarantineStore,
    ) -> None:
        # So an import that comes out wrong can be explained months later.
        await _ingest(engine, tenant_a, candidate_id, store, _pdf(" ".join(RESUME_LINES * 4)))

        async with tenant_transaction(engine, tenant_a) as session:
            row = (await session.execute(select(Upload))).scalars().one()
        assert "extractor=pdfplumber" in row.parse_detail

    async def test_the_upload_keeps_the_layout_and_the_diagnostic(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        store: QuarantineStore,
    ) -> None:
        # So a refused import can show what ARIA saw, and a fact's line can be quoted
        # after the original is deleted (RESUME_ENGINE §5.5).
        await _ingest(engine, tenant_a, candidate_id, store, _pdf(" ".join(RESUME_LINES * 4)))

        async with tenant_transaction(engine, tenant_a) as session:
            row = (await session.execute(select(Upload))).scalars().one()
        assert row.layout is not None
        layout = LayoutDocument.model_validate(row.layout, strict=False)
        assert layout.lines
        assert row.cross_extractor == "agree"

    async def test_a_clean_pdf_does_not_need_review(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        store: QuarantineStore,
    ) -> None:
        await _ingest(engine, tenant_a, candidate_id, store, _pdf(" ".join(RESUME_LINES * 4)))

        async with tenant_transaction(engine, tenant_a) as session:
            row = (await session.execute(select(Upload))).scalars().one()
        assert row.needs_review is False


class TestTheOriginalIsDeleted:
    async def test_by_default_the_original_does_not_survive(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        store: QuarantineStore,
    ) -> None:
        result = await _ingest(engine, tenant_a, candidate_id, store, _docx(RESUME_LINES), allow_docx=True)

        async with tenant_transaction(engine, tenant_a) as session:
            row = (await session.execute(select(Upload))).scalars().one()

        assert row.original_deleted_at is not None
        assert store.exists(row.quarantine_name) is False
        assert result.original_kept is False

    async def test_the_candidate_can_choose_to_keep_it(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        store: QuarantineStore,
    ) -> None:
        await _ingest(
            engine,
            tenant_a,
            candidate_id,
            store,
            _pdf(" ".join(RESUME_LINES * 4)),
            keep_original=True,
        )

        async with tenant_transaction(engine, tenant_a) as session:
            row = (await session.execute(select(Upload))).scalars().one()

        assert row.keep_original is True
        assert row.original_deleted_at is None
        assert store.exists(row.quarantine_name) is True


class TestRefusals:
    async def test_a_scan_is_refused_with_advice_and_recorded(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        store: QuarantineStore,
    ) -> None:
        result = await _ingest(engine, tenant_a, candidate_id, store, _scanned_pdf())

        assert result.accepted is False
        assert result.refusal is not None
        assert result.refusal.reason == "no_text_layer"
        assert result.refusal.stage == "parse"
        assert "no text layer" in result.refusal.advice
        assert "paste the text" in result.refusal.advice

        async with tenant_transaction(engine, tenant_a) as session:
            row = (await session.execute(select(Upload))).scalars().one()
        # The row survives the refusal, so "why was my upload rejected?" has an answer,
        # and it is recorded as refused rather than failed: a scan is a decision about
        # the file, not an error on our side.
        assert row.parse_status == "refused"
        assert "no_text_layer" in row.parse_detail
        assert "no text layer" in row.parse_detail

    async def test_the_http_layer_can_still_raise_after_committing(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        store: QuarantineStore,
    ) -> None:
        from aria_api.uploads.pipeline import UploadRefused

        result = await _ingest(engine, tenant_a, candidate_id, store, _scanned_pdf())
        with pytest.raises(UploadRefused, match="no_text_layer"):
            result.raise_if_refused()

    async def test_an_unsupported_type_is_refused_before_a_row_exists(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        store: QuarantineStore,
    ) -> None:
        # The uploads CHECK only permits the five accepted types, so there is nowhere
        # to put the row — but the attempt is still audited, and the audit event has to
        # survive, which it only does because the refusal is returned rather than
        # raised through the caller's transaction.
        result = await _ingest(engine, tenant_a, candidate_id, store, b"\x7fELF\x02\x01\x01\x00")
        assert result.refusal is not None
        assert result.refusal.reason == "unsupported_type"
        assert result.refusal.stage == "type"

        async with tenant_transaction(engine, tenant_a) as session:
            rows = (await session.execute(select(Upload))).scalars().all()
            events = await read_chain(session, tenant_a)

        assert rows == []
        assert len(events) == 1
        assert events[0].action == "upload.refused"
        assert events[0].payload["reason"] == "unsupported_type"

    async def test_a_refused_upload_leaves_no_file_behind(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        store: QuarantineStore,
    ) -> None:
        result = await _ingest(engine, tenant_a, candidate_id, store, b"\x00\x01\x02\x03binary")
        assert result.refusal is not None
        assert list(store.root.iterdir()) == []


class TestTheAuditTrail:
    async def test_each_step_is_recorded_in_order(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        store: QuarantineStore,
    ) -> None:
        await _ingest(engine, tenant_a, candidate_id, store, _docx(RESUME_LINES), allow_docx=True)

        async with tenant_transaction(engine, tenant_a) as session:
            actions = [event.action for event in await read_chain(session, tenant_a)]

        assert actions == [
            "upload.received",
            "upload.scanned",
            "upload.parsed",
            "upload.original_deleted",
        ]

    async def test_the_audit_payload_holds_no_resume_text(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        store: QuarantineStore,
    ) -> None:
        # The candidate's own name is S1 and belongs in the row, not in event payloads.
        await _ingest(engine, tenant_a, candidate_id, store, _docx(RESUME_LINES), allow_docx=True)

        async with tenant_transaction(engine, tenant_a) as session:
            events = await read_chain(session, tenant_a)

        rendered = " ".join(f"{event.payload}{event.reason}" for event in events)
        assert "Jordan" not in rendered
        assert "jordan.rivera@example.com" not in rendered
        assert "React components" not in rendered

    async def test_the_filename_is_recorded_but_only_its_length_is_audited(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        store: QuarantineStore,
    ) -> None:
        # A filename usually contains the candidate's name.
        await _ingest(
            engine,
            tenant_a,
            candidate_id,
            store,
            _docx(RESUME_LINES),
            filename="Jordan_Rivera_Resume_2026.docx",
            allow_docx=True,
        )

        async with tenant_transaction(engine, tenant_a) as session:
            row = (await session.execute(select(Upload))).scalars().one()
            events = await read_chain(session, tenant_a)

        assert row.original_filename == "Jordan_Rivera_Resume_2026.docx"
        received = next(event for event in events if event.action == "upload.received")
        assert received.payload["filename_length"] == len("Jordan_Rivera_Resume_2026.docx")
        assert "Jordan" not in str(received.payload)


class TestDuplicates:
    async def test_the_same_file_twice_is_recognised(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        store: QuarantineStore,
    ) -> None:
        content = _pdf(" ".join(RESUME_LINES * 4))
        first = await _ingest(engine, tenant_a, candidate_id, store, content)

        stored = await store.store_bytes(content)
        async with tenant_transaction(engine, tenant_a) as session:
            existing = await find_duplicate_upload(session, candidate_id=candidate_id, sha256=stored.sha256)

        assert existing == first.upload_id

    async def test_a_different_file_is_not_a_duplicate(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        candidate_id: uuid.UUID,
        store: QuarantineStore,
    ) -> None:
        await _ingest(engine, tenant_a, candidate_id, store, _pdf(" ".join(RESUME_LINES * 4)))

        other = await store.store_bytes(_pdf(" ".join([*RESUME_LINES, "Also shipped a CLI"] * 4)))
        async with tenant_transaction(engine, tenant_a) as session:
            assert (
                await find_duplicate_upload(session, candidate_id=candidate_id, sha256=other.sha256) is None
            )


class TestTenancy:
    async def test_another_tenant_cannot_see_the_upload(
        self,
        engine: AsyncEngine,
        tenant_a: TenantId,
        tenant_b: TenantId,
        candidate_id: uuid.UUID,
        store: QuarantineStore,
    ) -> None:
        await _ingest(engine, tenant_a, candidate_id, store, _docx(RESUME_LINES), allow_docx=True)
        async with tenant_transaction(engine, tenant_b) as session:
            assert (await session.execute(select(Upload))).scalars().all() == []


class TestATwoColumnPdfIsRecoveredNotMisread:
    """The layout case, through the pipeline the API actually uses.

    Both extractors flatten a visual row into a line and therefore agree with each
    other, so the cross-check cannot see this. The geometry can: a vertical band no
    text crosses, running the height of the page.
    """

    #: Hyphen bullets on the achievements, as a real resume has them: the importer
    #: reads a bullet as a claim and a bare line as a heading or an employer. A
    #: hyphen rather than a round glyph because this hand-built PDF encodes its
    #: strings as latin-1, where U+2022 does not exist.
    MAIN: ClassVar[list[str]] = [
        "EXPERIENCE",
        "Accenture - Software Engineer",
        "Jun 2024 - Present",
        "- Built 20+ React components used by 4 teams",
        "- Cut p95 API latency by 30% with Redis",
        "Zelis - Software Engineering Intern",
        "May 2023 - Aug 2023",
        "- Automated a manual claims report with Python",
    ]
    #: With its own heading, as a real sidebar has. (The parser's own test keeps a
    #: heading-free sidebar, because there the point is that the merged text looks
    #: plausible and only the geometry gives it away. Here the point is that the
    #: pipeline recovers the order and the import comes out right.)
    SIDEBAR: ClassVar[list[str]] = [
        "TECHNICAL SKILLS",
        "Python, TypeScript, C++",
        "AWS, Docker, Kubernetes",
        "PostgreSQL, Redis, Kafka",
        "React, FastAPI, Next.js",
        "PyTorch, scikit-learn",
        "Git, GitHub Actions",
        "Accessibility, WCAG",
        "Portland, OR",
    ]

    async def test_the_upload_records_which_pages_were_rebuilt(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, store: QuarantineStore
    ) -> None:
        result = await _ingest(
            engine, tenant_a, candidate_id, store, _two_column_pdf(self.MAIN, self.SIDEBAR)
        )
        assert result.parsed is not None
        assert result.parsed.column_pages == (1,)
        assert result.parsed.needs_review is True
        assert result.parsed.extractor == "pdfplumber"

        async with tenant_transaction(engine, tenant_a) as session:
            row = (
                await session.execute(
                    select(Upload.needs_review, Upload.parse_detail).where(Upload.id == result.upload_id)
                )
            ).first()
        assert row is not None
        assert row[0] is True
        assert "columns found by geometry" in row[1]

    async def test_the_extracted_text_is_in_reading_order(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, store: QuarantineStore
    ) -> None:
        result = await _ingest(
            engine, tenant_a, candidate_id, store, _two_column_pdf(self.MAIN, self.SIDEBAR)
        )
        assert result.parsed is not None
        lines = [line for line in result.parsed.text.splitlines() if line.strip()]
        # Whole left column before the sidebar, not a row at a time.
        assert lines.index("- Automated a manual claims report with Python") < lines.index(
            "Python, TypeScript, C++"
        )
        # And nothing was lost on the way.
        for line in self.MAIN + self.SIDEBAR:
            assert line in result.parsed.text

    async def test_the_import_reads_it_correctly(
        self, engine: AsyncEngine, tenant_a: TenantId, candidate_id: uuid.UUID, store: QuarantineStore
    ) -> None:
        # The payoff: with the order fixed, the roles and their bullets come out
        # right instead of the sidebar being spliced into the achievements.
        from aria_core.importer.resume import parse_resume
        from aria_core.taxonomy.loader import load_default_taxonomy

        result = await _ingest(
            engine, tenant_a, candidate_id, store, _two_column_pdf(self.MAIN, self.SIDEBAR)
        )
        content = result.extracted
        assert content is not None
        plan = parse_resume(content, load_default_taxonomy())

        assert [role.employer for role in plan.experiences] == ["Accenture", "Zelis"]
        bullets = [fact.text for fact in plan.experiences[0].facts]
        assert "Built 20+ React components used by 4 teams" in bullets
        # The sidebar did not end up inside a bullet.
        for text in bullets:
            assert "TypeScript" not in text or "React components" not in text
