"""Resumes: build one page for a posting, and read back the page, its reasons and its gaps.

Step 11b (DECISIONS 2026-11-08). ``POST /resumes`` builds the snapshot from approved rows,
hands it to the Resume Engine with the posting, and stores what comes back: the PDF, the
artifacts that explain and reproduce it, and an audit event. The engine never sees the
database; this route never writes a word of the page.

The posting is untrusted content. It reaches the engine as data and the database as data, and
never instruction text (SECURITY section 5). The model is used only when asked for, since it
costs money, and the gateway sends candidate data to it only for a tenant that opted in; a
refusal there leaves the page in the facts' own words.

The PDF downloads as ``First_Last_Resume.pdf``, from the approved name alone.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Any

from aria_resume.api import ResumeOptions, build_resume, file_name
from aria_resume.match.embeddings import Embedder, WordHashEmbedder
from aria_resume.render.pdf import ChromiumPrinter
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import Response
from sqlalchemy import select

from aria_api.deps import CallerDep, SessionDep, SettingsDep, TaxonomyDep
from aria_api.schemas import (
    ExplainedBullet,
    ExplainedFact,
    GapItem,
    ResumeDetail,
    ResumeExplanation,
    ResumeGaps,
    ResumeList,
    ResumeOut,
    ResumeRequest,
)
from aria_core.audit.log import append_event
from aria_core.db.models import Resume
from aria_core.graph.snapshot import build_candidate_snapshot
from aria_core.llm_client import GatewayLlmClient
from aria_core.schemas.audit import ActorKind, AuditAction, AuditEventDraft, AuditOutcome, SubjectKind
from aria_core.schemas.base import utc_now

__all__ = ["router"]

router = APIRouter(prefix="/resumes", tags=["resumes"])


def _embedder(local_model: bool) -> Embedder:
    if not local_model:
        return WordHashEmbedder()
    try:
        from aria_resume.match.onnx_embedder import OnnxEmbedder

        return OnnxEmbedder()
    except Exception as error:  # the ml group or the weights are missing
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the local embedding model is not installed (./tasks.ps1 models); "
            "or ask for local_model: false",
        ) from error


def _counts(artifacts: dict[str, Any]) -> dict[str, int]:
    findings = artifacts.get("validation_report", {}).get("findings", {})
    return {check: len(found) for check, found in findings.items()}


def _out(row: Resume) -> ResumeOut:
    return ResumeOut(
        id=row.id,
        status=row.status,
        fit=row.fit,
        file_name=row.file_name,
        used_model=row.used_model,
        created_at=row.created_at,
        findings=_counts(row.artifacts),
    )


async def _row(session: SessionDep, caller: CallerDep, resume_id: uuid.UUID) -> Resume:
    found = await session.execute(
        select(Resume).where(Resume.id == resume_id, Resume.candidate_id == caller.candidate_id)
    )
    row = found.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such resume")
    return row


@router.post("", response_model=ResumeOut, status_code=status.HTTP_201_CREATED)
async def create(
    body: ResumeRequest, caller: CallerDep, session: SessionDep, settings: SettingsDep, taxonomy: TaxonomyDep
) -> ResumeOut:
    today = utc_now().date()
    snapshot = await build_candidate_snapshot(session, caller.candidate_id, taxonomy=taxonomy, now=today)
    if not snapshot.contact.full_name:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="approve a name first: a page is never printed without one",
        )
    embedder = _embedder(body.local_model)
    llm = None
    if body.use_model:
        if settings.llm_gateway_service_token is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="use_model needs ARIA_LLM_GATEWAY_SERVICE_TOKEN and a running gateway",
            )
        llm = GatewayLlmClient(
            base_url=settings.llm_gateway_url,
            service_token=settings.llm_gateway_service_token,
            tenant_id=caller.tenant_id,
            run_id=uuid.uuid4(),
        )
    # The engine prints through Chromium with asyncio.run, so it runs in a thread of its own.
    result = await asyncio.to_thread(
        build_resume,
        body.jd_text,
        snapshot,
        ResumeOptions(today=today),
        llm,
        taxonomy=taxonomy,
        embedder=embedder,
        printer=ChromiumPrinter(),
    )
    artifacts = result.artifacts()
    # Plus each cited fact's text as the snapshot held it, so the page can be explained in
    # the words it was built from.
    cited = {fact_id for bullet in artifacts["bullets"] for fact_id in bullet["fact_ids"]}
    artifacts["facts"] = {
        str(fact.fact_id): fact.text for fact in snapshot.facts if str(fact.fact_id) in cited
    }
    row = Resume(
        tenant_id=caller.tenant_id,
        candidate_id=caller.candidate_id,
        created_by=caller.user_id,
        status=result.status,
        fit=result.evidence.fit_decision.value,
        jd_text=body.jd_text,
        jd_sha256=hashlib.sha256(body.jd_text.encode("utf-8")).hexdigest(),
        input_hash=result.input_hash,
        used_model=llm is not None,
        versions=result.versions,
        artifacts=artifacts,
        pdf=result.pdf,
        pdf_sha256=hashlib.sha256(result.pdf).hexdigest(),
        file_name=file_name(snapshot),
    )
    session.add(row)
    await session.flush()
    await append_event(
        session,
        AuditEventDraft(
            action=AuditAction.RESUME_BUILT,
            actor_kind=ActorKind.USER,
            actor_id=caller.actor_id,
            subject_kind=SubjectKind.RESUME,
            subject_id=str(row.id),
            outcome=AuditOutcome.OK,
            reason="a resume page built for a posting",
            payload={
                "status": result.status,
                "fit": row.fit,
                "used_model": row.used_model,
                "input_hash": result.input_hash,
                "jd_sha256": row.jd_sha256,
            },
        ),
        tenant_id=caller.tenant_id,
    )
    await session.refresh(row)
    return _out(row)


@router.get("", response_model=ResumeList)
async def list_resumes(caller: CallerDep, session: SessionDep) -> ResumeList:
    found = await session.execute(
        select(Resume).where(Resume.candidate_id == caller.candidate_id).order_by(Resume.created_at.desc())
    )
    return ResumeList(resumes=[_out(row) for row in found.scalars()])


@router.get("/{resume_id}", response_model=ResumeDetail)
async def detail(resume_id: uuid.UUID, caller: CallerDep, session: SessionDep) -> ResumeDetail:
    row = await _row(session, caller, resume_id)
    return ResumeDetail(
        **_out(row).model_dump(),
        input_hash=row.input_hash,
        versions=row.versions,
        validation_report=row.artifacts.get("validation_report", {}),
    )


@router.get("/{resume_id}/pdf")
async def pdf(resume_id: uuid.UUID, caller: CallerDep, session: SessionDep) -> Response:
    row = await _row(session, caller, resume_id)
    return Response(
        content=row.pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{row.file_name}"'},
    )


@router.get("/{resume_id}/explain", response_model=ResumeExplanation)
async def explain(resume_id: uuid.UUID, caller: CallerDep, session: SessionDep) -> ResumeExplanation:
    """Each bullet with the facts it says, in their words, and what its slot was planned for."""
    row = await _row(session, caller, resume_id)
    artifacts: dict[str, Any] = row.artifacts
    labels = {item["id"]: item["label"] for item in artifacts.get("requirements", [])}
    planned = {
        tuple(slot["fact_ids"]): [
            labels.get(requirement, requirement) for requirement in slot["requirement_ids"]
        ]
        for section in artifacts.get("plan", {}).get("sections", [])
        for entry in section.get("entries", [])
        for slot in entry.get("bullets", [])
    }
    # The facts as they read when the page was built, kept with it: a later edit changes the
    # graph, not what this page said.
    texts: dict[str, str] = artifacts.get("facts", {})
    return ResumeExplanation(
        bullets=[
            ExplainedBullet(
                text=bullet["text"],
                facts=[
                    ExplainedFact(id=uuid.UUID(fact_id), text=texts.get(fact_id, ""))
                    for fact_id in bullet["fact_ids"]
                ],
                requirements=planned.get(tuple(bullet["fact_ids"]), []),
            )
            for bullet in artifacts.get("bullets", [])
        ]
    )


@router.get("/{resume_id}/gaps", response_model=ResumeGaps)
async def gaps(resume_id: uuid.UUID, caller: CallerDep, session: SessionDep) -> ResumeGaps:
    row = await _row(session, caller, resume_id)
    evidence: dict[str, Any] = row.artifacts.get("evidence_map", {})  # type: ignore[assignment]

    def items(key: str) -> list[GapItem]:
        return [
            GapItem(
                label=gap["label"],
                must_have=gap["must_have"],
                best_score=gap["best_score"],
                suggestion=gap["suggestion"],
            )
            for gap in evidence.get(key, [])
        ]

    return ResumeGaps(
        coverage=evidence.get("coverage", 0.0),
        fit=row.fit,
        missing=items("missing"),
        light=items("light_gaps"),
    )
