"""Call-recording upload to S3 (K, PRD §11 audit + 30-day retention).

Buffers are uploaded on call end; the object key mirrors the ``recording_url``
persisted on the CallLog. boto3 is imported lazily so the dependency is optional
in dev — with no bucket configured, upload is a logged no-op (seam), and the
compliance `recording_url` still resolves to the intended object path.

Retention is enforced two ways: an S3 lifecycle rule (Terraform, infra/) expires
objects after N days, and `scripts/retention_cleanup.py` purges the DB rows.
"""

from __future__ import annotations

from koel.common.config import CommonSettings, get_common_settings
from koel.common.logging import get_logger

logger = get_logger("koel.voicebot.recording")


def recording_key(session_id: str) -> str:
    return f"recordings/{session_id}.wav"


async def upload_recording(
    session_id: str, wav: bytes, settings: CommonSettings | None = None
) -> str | None:
    """Upload a call WAV to S3; return its s3:// URL, or None if disabled/failed.

    Best-effort: an upload failure is logged and swallowed so it never fails the
    call-teardown path (the transcript + metadata still persist).
    """
    settings = settings or get_common_settings()
    key = recording_key(session_id)
    url = f"s3://{settings.s3_recording_bucket}/{key}"

    if not settings.s3_recording_bucket:
        logger.info("recording_upload_skipped (no bucket configured) session_id=%s", session_id)
        return None
    try:
        import boto3  # lazy: optional dependency, only needed when recording is on

        client = boto3.client("s3", region_name=settings.s3_region)
        client.put_object(
            Bucket=settings.s3_recording_bucket,
            Key=key,
            Body=wav,
            ContentType="audio/wav",
        )
        logger.info("recording_uploaded session_id=%s bytes=%d", session_id, len(wav))
        return url
    except Exception:  # noqa: BLE001 — never fail teardown on a recording upload
        logger.error("recording_upload_failed session_id=%s", session_id, exc_info=True)
        return None
