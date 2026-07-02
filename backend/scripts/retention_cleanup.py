"""Purge call data past the retention window (K, PRD §11 30-day retention).

Deletes CallLog rows (and their TranscriptTurns via cascade) older than
``recording_retention_days``. S3 objects are expired independently by the bucket
lifecycle rule (Terraform). Idempotent — safe to run daily from a cron/K8s Job.

Run: python -m scripts.retention_cleanup [--days N] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select

from koel.common.config import get_common_settings
from koel.db.models import CallLog
from koel.db.session import session_scope


async def cleanup(days: int | None = None, *, dry_run: bool = False) -> int:
    settings = get_common_settings()
    days = days if days is not None else settings.recording_retention_days
    cutoff = datetime.now(UTC) - timedelta(days=days)

    async with session_scope() as db:
        stale = (
            await db.execute(select(func.count()).select_from(CallLog).where(CallLog.started_at < cutoff))
        ).scalar_one()
        if not dry_run and stale:
            # Transcript turns cascade via the CallLog relationship FK.
            await db.execute(delete(CallLog).where(CallLog.started_at < cutoff))
    verb = "would delete" if dry_run else "deleted"
    print(f"retention_cleanup: {verb} {stale} call_logs older than {days}d (cutoff {cutoff.isoformat()})")
    return stale


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    asyncio.run(cleanup(args.days, dry_run=args.dry_run))
