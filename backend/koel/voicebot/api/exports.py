"""Regulatory audit-export API (6.5, PRD §10/§11).

An operator exports the banked call record for a date range as a signed JSONL
bundle. With object storage configured the bundle is uploaded and a presigned
URL returned; otherwise it comes back inline with its SHA-256 so the audit trail
holds either way. Tenant-scoped (AF); each export is itself logged as an
``ExportJob`` row.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, status
from pydantic import BaseModel
from sqlalchemy import select

from koel.common.auth import CurrentUser
from koel.common.logging import get_logger
from koel.db.models import ExportJob
from koel.db.session import session_scope
from koel.voicebot.exports import build_export_bundle, upload_export

logger = get_logger("koel.voicebot.exports_api")
router = APIRouter(prefix="/v1/exports", tags=["exports"])


class ExportRequest(BaseModel):
    start: datetime | None = None  # inclusive; None ⇒ from the beginning
    end: datetime | None = None  # exclusive; None ⇒ up to now


class ExportResult(BaseModel):
    job_id: str
    record_count: int
    manifest_sha256: str
    object_url: str | None = None  # presigned S3 URL when storage is configured
    bundle: str | None = None  # inline JSONL when it isn't (dev / no bucket)


class ExportJobOut(BaseModel):
    id: str
    requested_by: str | None = None
    range_start: str | None = None
    range_end: str | None = None
    record_count: int
    manifest_sha256: str
    object_url: str | None = None
    created_at: str | None = None


@router.post("", response_model=ExportResult, status_code=status.HTTP_201_CREATED)
async def create_export(payload: ExportRequest, user: CurrentUser) -> ExportResult:
    """Build + (best-effort) store a regulatory export for the caller's org."""
    data, count, sha = await build_export_bundle(
        org_id=user.org_id, start=payload.start, end=payload.end
    )
    async with session_scope() as db:
        job = ExportJob(
            org_id=user.org_id,
            requested_by=user.sub,
            range_start=payload.start,
            range_end=payload.end,
            record_count=count,
            manifest_sha256=sha,
        )
        db.add(job)
        await db.flush()
        job_id = job.id
        url = upload_export(job_id, data)
        job.object_url = url
    logger.info(
        "export_created",
        extra={"extra_fields": {
            "job_id": job_id, "org_id": user.org_id, "records": count, "stored": bool(url),
        }},
    )
    return ExportResult(
        job_id=job_id,
        record_count=count,
        manifest_sha256=sha,
        object_url=url,
        bundle=None if url else data.decode("utf-8"),
    )


@router.get("", response_model=list[ExportJobOut])
async def list_exports(user: CurrentUser) -> list[ExportJobOut]:
    """Prior exports for the caller's org, newest first (audit trail)."""
    async with session_scope() as db:
        stmt = select(ExportJob)
        if user.org_id is not None:
            stmt = stmt.where(ExportJob.org_id == user.org_id)
        stmt = stmt.order_by(ExportJob.created_at.desc()).limit(200)
        rows = (await db.execute(stmt)).scalars().all()
        return [
            ExportJobOut(
                id=r.id,
                requested_by=r.requested_by,
                range_start=r.range_start.isoformat() if r.range_start else None,
                range_end=r.range_end.isoformat() if r.range_end else None,
                record_count=r.record_count,
                manifest_sha256=r.manifest_sha256,
                object_url=r.object_url,
                created_at=r.created_at.isoformat() if r.created_at else None,
            )
            for r in rows
        ]
