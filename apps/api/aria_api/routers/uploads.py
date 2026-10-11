"""Uploading a resume, and importing it.

Two endpoints rather than one, deliberately. ``POST /uploads`` runs the pipeline from
SECURITY.md §7 and stops with the extracted text; ``POST /uploads/{id}/import`` reads
that text into the graph as drafts. Splitting them means a file can be uploaded,
looked at, and imported later — and it means the slow, hostile part (parsing somebody
else's PDF in a subprocess) is over before anything touches the candidate's graph.

A refusal is **200 with a reason**, not a 4xx. The uploads row and its audit events
are the product of ingestion even for a file that was turned away, and the client
needs the row id to show the candidate what happened to it. A 4xx would be correct
about the file and wrong about the request, which succeeded in doing exactly what it
was asked to do.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from sqlalchemy import select

from aria_api.deps import CallerDep, ParserDep, ScannerDep, SessionDep, SettingsDep, StoreDep, TaxonomyDep
from aria_api.schemas import ImportResponse, UploadResponse
from aria_api.uploads.pipeline import find_duplicate_upload, ingest_upload
from aria_core.db.models import Upload
from aria_core.importer.apply import apply_import_plan
from aria_core.importer.notes import parse_skills_notes
from aria_core.importer.resume import parse_resume
from aria_core.schemas.untrusted import UntrustedContent, UntrustedSourceKind

__all__ = ["router"]

router = APIRouter(prefix="/uploads", tags=["uploads"])


@router.post("", response_model=UploadResponse, status_code=status.HTTP_201_CREATED)
async def upload(
    caller: CallerDep,
    session: SessionDep,
    settings: SettingsDep,
    store: StoreDep,
    scanner: ScannerDep,
    parser: ParserDep,
    file: Annotated[UploadFile, File()],
    keep_original: Annotated[bool, Query()] = False,
) -> UploadResponse:
    """Quarantine, size-check, type-check, scan, parse. Nothing is imported yet."""
    content = await file.read()
    stored = await store.store_bytes(content)

    # Uploading the same file twice is a thing people do. The uploads table has a
    # unique index on (candidate_id, sha256) for exactly this, so without the check
    # the second attempt reaches the client as an integrity error.
    existing = await find_duplicate_upload(session, candidate_id=caller.candidate_id, sha256=stored.sha256)
    if existing is not None:
        store.delete(stored.name)
        row = (
            await session.execute(
                select(
                    Upload.detected_type,
                    Upload.parse_status,
                    Upload.needs_review,
                    Upload.parse_detail,
                    Upload.extracted_text,
                ).where(Upload.id == existing)
            )
        ).first()
        assert row is not None  # the id came from this candidate's own rows
        return UploadResponse(
            upload_id=existing,
            detected_type=row[0],
            parse_status=row[1],
            needs_review=bool(row[2]),
            characters=len(row[4] or ""),
            detail=f"already uploaded; nothing reprocessed. {row[3]}",
            duplicate_of=existing,
        )

    result = await ingest_upload(
        session,
        tenant_id=caller.tenant_id,
        candidate_id=caller.candidate_id,
        # The client's filename is recorded so the candidate recognises the file and
        # is never used to decide anything, least of all a path.
        original_filename=file.filename or "upload",
        stored=stored,
        store=store,
        scanner=scanner,
        parser=parser,
        keep_original=keep_original,
        allow_docx=settings.allow_docx_uploads,
    )

    if result.refusal is not None:
        return UploadResponse(
            upload_id=result.upload_id,
            detected_type=result.detected_type.value if result.detected_type else None,
            parse_status="refused",
            refusal_reason=result.refusal.reason,
            refusal_advice=result.refusal.advice,
        )

    parsed = result.parsed
    assert parsed is not None  # accepted implies parsed
    return UploadResponse(
        upload_id=result.upload_id,
        detected_type=result.detected_type.value if result.detected_type else None,
        parse_status="parsed",
        needs_review=parsed.needs_review,
        column_pages=list(parsed.column_pages),
        extractor=parsed.extractor,
        characters=len(parsed.text),
        links=list(parsed.links),
        detail=parsed.detail,
    )


@router.post("/{upload_id}/import", response_model=ImportResponse)
async def import_upload(
    upload_id: uuid.UUID,
    caller: CallerDep,
    session: SessionDep,
    taxonomy: TaxonomyDep,
    as_notes: Annotated[
        bool,
        Query(description="Read the file as a plain skills list rather than as a resume."),
    ] = False,
) -> ImportResponse:
    """Read an upload's extracted text into the graph as drafts.

    Everything written is a draft, every draft carries its upload and line, and
    anything the importer could not read comes back as a question.
    """
    row = (
        await session.execute(
            select(Upload.extracted_text, Upload.parse_status, Upload.candidate_id).where(
                Upload.id == upload_id
            )
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such upload")
    if row[2] != caller.candidate_id:
        # Row-level security already confines this to the tenant; this confines it to
        # the candidate, which matters the moment a tenant has more than one.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such upload")
    if row[1] != "parsed" or not row[0]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"this upload is {row[1]}, so there is no text to import",
        )

    content = UntrustedContent(
        source_kind=UntrustedSourceKind.UPLOADED_DOCUMENT,
        source_ref=f"upload:{upload_id}",
        text=row[0],
    )
    plan = parse_skills_notes(content, taxonomy) if as_notes else parse_resume(content, taxonomy)
    outcome = await apply_import_plan(
        session,
        plan,
        tenant_id=caller.tenant_id,
        candidate_id=caller.candidate_id,
        upload_id=upload_id,
        actor_id=caller.actor_id,
    )

    return ImportResponse(
        upload_id=upload_id,
        counts={**plan.counts(), **outcome.counts()},
        summary=plan.summary_lines() + outcome.summary_lines(),
        questions_queued=len(outcome.question_ids),
        held_back=[f"{label}: {question.prompt}" for label, question in outcome.held_back],
    )
