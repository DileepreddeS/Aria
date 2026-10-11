"""The upload pipeline (SECURITY.md §7).

::

    upload -> quarantine -> size limit -> magic-byte type check -> hash
           -> malware scan -> parse in an isolated worker -> extracted text
           -> delete the original unless the user keeps it

Each step is a function with its own test, and the order is the order in the spec: a
file is on disk under a generated name before anything inspects it, and nothing is
inspected by a step that has not been told the previous one passed.

**A refusal is a return value, not an exception.** That is not a style preference. The
``uploads`` row and the audit events *are* the product of ingestion — including for a
file that was turned away — and raising through the caller's transaction rolls them
back, so every refusal would quietly lose the record explaining it. Returning the
outcome lets the caller commit first and then decide what to report.

:class:`UploadRefused` still exists, for the HTTP layer to raise *after* committing.

Phase 1's sanitized derivative is the extracted text. The regenerated PDF that
SECURITY.md §7 also describes arrives with the Phase 2 renderer, rather than being
built twice.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

import structlog
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aria_api.uploads.detect import DetectedType, UnsupportedFileType, detect_type
from aria_api.uploads.quarantine import QuarantineStore, StoredFile
from aria_api.uploads.scanner import MalwareScanner, ScanResult, ScanStatus
from aria_core.audit.log import append_event
from aria_core.db.models import Upload
from aria_core.schemas.audit import (
    ActorKind,
    AuditAction,
    AuditEventDraft,
    AuditOutcome,
    SubjectKind,
)
from aria_core.schemas.base import utc_now
from aria_core.schemas.identity import TenantId, new_id
from aria_core.schemas.untrusted import UntrustedContent, UntrustedSourceKind
from aria_document_parser.client import ParsedDocument
from aria_document_parser.errors import ParseRefused

__all__ = [
    "DocumentParser",
    "UploadRefusal",
    "UploadRefused",
    "UploadResult",
    "find_duplicate_upload",
    "ingest_upload",
]

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class UploadRefusal:
    """Why a file was turned away, and what to tell the candidate.

    ``reason`` is the stable code that goes into the row, the audit payload and any
    metric. ``advice`` is the sentence shown to a person, which says what to do next —
    "parse error" is not something anyone can act on.
    """

    reason: str
    advice: str
    #: Which gate turned it away: "type", "scan" or "parse".
    stage: str

    def __str__(self) -> str:
        return f"{self.reason}: {self.advice}"


class UploadRefused(Exception):
    """Raised by the HTTP layer once the refusal has been committed."""

    def __init__(self, refusal: UploadRefusal) -> None:
        super().__init__(str(refusal))
        self.refusal = refusal

    @property
    def reason(self) -> str:
        return self.refusal.reason

    @property
    def detail(self) -> str:
        return self.refusal.advice


class DocumentParser(Protocol):
    """Parses a quarantined file. Implemented by services/document_parser.

    The detected type crosses as a plain string: the parser is a separate service and
    has no business importing this package's enum. Its result type is reused rather
    than mirrored, so there is one ParsedDocument at this boundary instead of two
    identical ones that could drift.
    """

    async def parse(self, content: bytes, detected_type: str) -> ParsedDocument: ...


@dataclass(frozen=True, slots=True)
class UploadResult:
    """What happened to one file. ``refusal`` set means it was turned away."""

    upload_id: uuid.UUID
    detected_type: DetectedType | None
    scan: ScanResult | None
    parsed: ParsedDocument | None
    original_kept: bool
    refusal: UploadRefusal | None = None

    @property
    def accepted(self) -> bool:
        return self.refusal is None and self.parsed is not None

    def raise_if_refused(self) -> None:
        """For the HTTP layer, after the transaction has committed."""
        if self.refusal is not None:
            raise UploadRefused(self.refusal)

    @property
    def extracted(self) -> UntrustedContent | None:
        """The derivative, as untrusted content ready for the importer."""
        if self.parsed is None:
            return None
        return UntrustedContent(
            source_kind=UntrustedSourceKind.UPLOADED_DOCUMENT,
            source_ref=f"upload:{self.upload_id}",
            text=self.parsed.text,
        )


async def _audit(
    session: AsyncSession,
    *,
    tenant_id: TenantId,
    subject_id: str,
    action: AuditAction,
    outcome: AuditOutcome,
    payload: dict[str, object],
    reason: str = "",
) -> None:
    await append_event(
        session,
        AuditEventDraft(
            action=action,
            actor_kind=ActorKind.USER,
            actor_id=subject_id,
            subject_kind=SubjectKind.UPLOAD,
            subject_id=subject_id,
            outcome=outcome,
            reason=reason,
            payload=payload,
        ),
        tenant_id=tenant_id,
    )


async def ingest_upload(
    session: AsyncSession,
    *,
    tenant_id: TenantId,
    candidate_id: uuid.UUID,
    original_filename: str,
    stored: StoredFile,
    store: QuarantineStore,
    scanner: MalwareScanner,
    parser: DocumentParser,
    keep_original: bool = False,
    allow_docx: bool = False,
) -> UploadResult:
    """Run a quarantined file through the rest of the pipeline.

    The file is already in quarantine — the size limit has to be enforced while
    streaming, so that step belongs to the store. ``original_filename`` is recorded so
    the candidate recognises the file and is never used to decide anything.

    Returns an :class:`UploadResult`; a refusal is carried on it rather than raised,
    so the caller commits the record of what happened before reporting it.
    """
    upload_id = new_id()

    # ---- type, from the content
    try:
        detected = detect_type(stored.content, allow_docx=allow_docx)
    except UnsupportedFileType as error:
        store.delete(stored.name)
        refusal = UploadRefusal("unsupported_type", error.advice, "type")
        # No row: the uploads CHECK only permits the five accepted types, and this is
        # not one of them. The attempt is still audited — someone uploading the wrong
        # thing three times is worth being able to see.
        await _audit(
            session,
            tenant_id=tenant_id,
            subject_id=str(upload_id),
            action=AuditAction.UPLOAD_REFUSED,
            outcome=AuditOutcome.DENIED,
            payload={
                "reason": refusal.reason,
                "stage": refusal.stage,
                "size_bytes": stored.size_bytes,
                "sha256": stored.sha256,
                "filename_length": len(original_filename),
            },
            reason=error.detail,
        )
        logger.info("upload_refused", reason=refusal.reason, stage=refusal.stage)
        return UploadResult(
            upload_id=upload_id,
            detected_type=None,
            scan=None,
            parsed=None,
            original_kept=False,
            refusal=refusal,
        )

    # ---- the row exists from here on, so every later outcome has somewhere to live
    await session.execute(
        insert(Upload).values(
            id=upload_id,
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            original_filename=original_filename[:255],
            detected_type=detected.value,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            quarantine_name=stored.name,
            scan_status=ScanStatus.NOT_SCANNED.value,
            scan_detail="pending",
            parse_status="pending",
            keep_original=keep_original,
        )
    )
    await _audit(
        session,
        tenant_id=tenant_id,
        subject_id=str(upload_id),
        action=AuditAction.UPLOAD_RECEIVED,
        outcome=AuditOutcome.OK,
        payload={
            "detected_type": detected.value,
            "size_bytes": stored.size_bytes,
            "sha256": stored.sha256,
            # The client's filename is S1 and goes in the row, not the audit payload.
            "filename_length": len(original_filename),
        },
    )

    # ---- malware scan
    scan = await scanner.scan(stored.content)
    await session.execute(
        update(Upload)
        .where(Upload.id == upload_id)
        .values(scan_status=scan.status.value, scan_detail=scan.detail[:200])
    )
    await _audit(
        session,
        tenant_id=tenant_id,
        subject_id=str(upload_id),
        action=AuditAction.UPLOAD_SCANNED,
        outcome=AuditOutcome.OK if scan.status.may_be_parsed else AuditOutcome.DENIED,
        payload={"scan_status": scan.status.value, "scanner": scanner.name},
        reason=scan.detail,
    )

    if not scan.status.may_be_parsed:
        store.delete(stored.name)
        refusal = UploadRefusal(f"scan_{scan.status.value}", scan.detail, "scan")
        await session.execute(
            update(Upload)
            .where(Upload.id == upload_id)
            .values(
                parse_status="refused",
                parse_detail=str(refusal)[:2000],
                original_deleted_at=utc_now(),
            )
        )
        logger.warning("upload_refused", reason=refusal.reason, stage=refusal.stage)
        return UploadResult(
            upload_id=upload_id,
            detected_type=detected,
            scan=scan,
            parsed=None,
            original_kept=False,
            refusal=refusal,
        )

    # ---- parse, in the isolated worker
    try:
        parsed = await parser.parse(stored.content, detected.value)
    except ParseRefused as error:
        # A refusal is a decision with advice for the candidate, not an internal
        # error. Recording it as "failed" would file a user-facing explanation where
        # nobody looks for one, and would hide real failures among the ordinary
        # "this is a scan" cases.
        refusal = UploadRefusal(error.reason, error.advice, "parse")
        await session.execute(
            update(Upload)
            .where(Upload.id == upload_id)
            .values(parse_status="refused", parse_detail=str(refusal)[:2000])
        )
        await _audit(
            session,
            tenant_id=tenant_id,
            subject_id=str(upload_id),
            action=AuditAction.UPLOAD_REFUSED,
            outcome=AuditOutcome.DENIED,
            payload={"reason": refusal.reason, "stage": refusal.stage},
            reason=refusal.advice,
        )
        logger.info("upload_refused", reason=refusal.reason, stage=refusal.stage)
        return UploadResult(
            upload_id=upload_id,
            detected_type=detected,
            scan=scan,
            parsed=None,
            original_kept=keep_original,
            refusal=refusal,
        )
    except Exception as error:
        # A failure is ours to investigate, so it is recorded and re-raised: unlike a
        # refusal, there is nothing useful to tell the candidate.
        await session.execute(
            update(Upload)
            .where(Upload.id == upload_id)
            .values(parse_status="failed", parse_detail=f"{type(error).__name__}: {error}"[:2000])
        )
        await _audit(
            session,
            tenant_id=tenant_id,
            subject_id=str(upload_id),
            action=AuditAction.UPLOAD_PARSED,
            outcome=AuditOutcome.FAILED,
            payload={"error": type(error).__name__},
            reason=str(error)[:500],
        )
        logger.error("upload_parse_failed", error=type(error).__name__)
        raise

    await session.execute(
        update(Upload)
        .where(Upload.id == upload_id)
        .values(
            parse_status="parsed",
            parse_detail=parsed.detail[:2000],
            extracted_text=parsed.text,
            needs_review=parsed.needs_review,
        )
    )
    await _audit(
        session,
        tenant_id=tenant_id,
        subject_id=str(upload_id),
        action=AuditAction.UPLOAD_PARSED,
        outcome=AuditOutcome.OK,
        payload={
            "characters": len(parsed.text),
            "links": len(parsed.links),
            "extractor": parsed.extractor,
            "needs_review": parsed.needs_review,
            "column_pages": len(parsed.column_pages),
            "detail": parsed.detail[:200],
        },
    )

    # ---- the original goes, unless the candidate asked to keep it
    if not keep_original:
        store.delete(stored.name)
        await session.execute(
            update(Upload).where(Upload.id == upload_id).values(original_deleted_at=utc_now())
        )
        await _audit(
            session,
            tenant_id=tenant_id,
            subject_id=str(upload_id),
            action=AuditAction.UPLOAD_ORIGINAL_DELETED,
            outcome=AuditOutcome.OK,
            payload={"reason": "default: the derivative is enough"},
        )

    return UploadResult(
        upload_id=upload_id,
        detected_type=detected,
        scan=scan,
        parsed=parsed,
        original_kept=keep_original,
    )


async def find_duplicate_upload(
    session: AsyncSession, *, candidate_id: uuid.UUID, sha256: str
) -> uuid.UUID | None:
    """An earlier upload of the identical file, if there is one.

    Byte-identical, so this is not near-duplicate detection — that works on extracted
    facts and lives in the importer. This just avoids processing the same PDF twice
    when someone clicks upload again.
    """
    return await session.scalar(
        select(Upload.id).where(Upload.candidate_id == candidate_id, Upload.sha256 == sha256)
    )
