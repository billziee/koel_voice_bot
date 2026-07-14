"""Regulatory audit-export bundles (6.5, PRD §10/§11).

Serialises the banked call record (calls + transcripts + outcome + the consent/
summary rollups on ``CallLog.metrics``) for a date range into JSONL, with a
SHA-256 manifest so a regulator can verify the bundle wasn't altered. Pure egress
over existing tables — no new data capture.

Storage mirrors ``recording.py``: with an S3 bucket configured the bundle is
uploaded and a presigned URL returned; with none (dev / empty key), the caller
gets the bundle inline plus its hash. boto3 stays a lazy, optional import.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from koel.common.config import CommonSettings, get_common_settings
from koel.common.logging import get_logger
from koel.db.models import CallLog
from koel.db.session import session_scope

logger = get_logger("koel.voicebot.exports")

_PRESIGN_TTL_S = 3600  # 1h — long enough to download, short enough to not linger


def _call_record(call: CallLog) -> dict:
    """One call, fully denormalised for the audit bundle (nothing else needed)."""
    turns = sorted(call.transcript_turns, key=lambda t: t.turn_index)
    return {
        "id": call.id,
        "org_id": call.org_id,
        "session_id": call.session_id,
        "bot_id": call.bot_id,
        "customer_id": call.customer_id,
        "direction": call.direction.value,
        "language": call.language,
        "outcome": call.outcome.value,
        "started_at": call.started_at.isoformat() if call.started_at else None,
        "ended_at": call.ended_at.isoformat() if call.ended_at else None,
        "duration_ms": call.duration_ms,
        "escalated": call.escalated,
        "recording_url": call.recording_url,
        "metrics": call.metrics or {},  # consent log, summary, latency, trace id
        "transcript": [
            {
                "turn_index": t.turn_index,
                "speaker": t.speaker,
                "text": t.text,
                "language": t.language,
                "intent": t.intent,
                "tone": t.tone,
                "interrupted": t.interrupted,
                "confidence": t.confidence,
            }
            for t in turns
        ],
    }


async def build_export_bundle(
    *, org_id: str | None, start: datetime | None, end: datetime | None
) -> tuple[bytes, int, str]:
    """Return ``(jsonl_bytes, record_count, sha256_hex)`` for calls started in
    ``[start, end)``. Org-scoped when the caller is tenant-bound (AF). Newline-
    delimited JSON so the bundle streams and diffs cleanly."""
    async with session_scope() as db:
        stmt = select(CallLog).options(selectinload(CallLog.transcript_turns))
        if org_id is not None:
            stmt = stmt.where(CallLog.org_id == org_id)
        if start is not None:
            stmt = stmt.where(CallLog.started_at >= start)
        if end is not None:
            stmt = stmt.where(CallLog.started_at < end)
        stmt = stmt.order_by(CallLog.started_at.asc())
        calls = (await db.execute(stmt)).scalars().all()
        lines = [json.dumps(_call_record(c), separators=(",", ":"), sort_keys=True) for c in calls]

    payload = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
    sha256 = hashlib.sha256(payload).hexdigest()
    return payload, len(calls), sha256


def upload_export(
    job_id: str, payload: bytes, settings: CommonSettings | None = None
) -> str | None:
    """Upload the bundle to S3 and return a presigned URL, or None when no bucket
    is configured (dev/empty key) or the upload fails — the API then returns the
    bundle inline. Best-effort, mirrors recording.upload_recording."""
    settings = settings or get_common_settings()
    if not settings.s3_recording_bucket:
        logger.info("export_upload_skipped (no bucket configured) job_id=%s", job_id)
        return None
    key = f"exports/{job_id}.jsonl"
    try:
        import boto3  # lazy: optional dependency

        client = boto3.client("s3", region_name=settings.s3_region)
        client.put_object(
            Bucket=settings.s3_recording_bucket,
            Key=key,
            Body=payload,
            ContentType="application/x-ndjson",
        )
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.s3_recording_bucket, "Key": key},
            ExpiresIn=_PRESIGN_TTL_S,
        )
        logger.info("export_uploaded job_id=%s bytes=%d", job_id, len(payload))
        return url
    except Exception:  # noqa: BLE001 — degrade to inline delivery, never 500 the export
        logger.error("export_upload_failed job_id=%s", job_id, exc_info=True)
        return None
