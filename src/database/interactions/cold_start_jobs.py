"""Cold-start work queue interactions."""

from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import and_, or_
from sqlmodel import Session, select

from src.database.interactions._shared import (
    RecordNotFoundError,
    dialect_insert,
    run,
    schema_values,
)
from src.database.models import ColdStartJob, utc_now
from src.database.schemas import ColdStartJobCreate

__all__ = ["enqueue", "claim", "retry", "complete"]


def _enqueue(session: Session, data: ColdStartJobCreate) -> ColdStartJob:
    values = schema_values(data)
    now = utc_now()
    for field in ("next_attempt_at", "created_at", "updated_at"):
        values.setdefault(field, now)
    statement = dialect_insert(session, ColdStartJob).values(**values)
    session.execute(statement.on_conflict_do_nothing(index_elements=["user_id"]))
    session.flush()
    return session.get(ColdStartJob, data.user_id)


def _claim(session: Session, *, lease_seconds: int) -> ColdStartJob | None:
    now = utc_now()
    statement = (
        select(ColdStartJob)
        .where(
            ColdStartJob.next_attempt_at <= now,
            or_(ColdStartJob.status == "pending", and_(ColdStartJob.status == "processing", ColdStartJob.lease_until <= now)),
        )
        .order_by(ColdStartJob.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = session.exec(statement).first()
    if job is None:
        return None
    job.status = "processing"
    job.attempts += 1
    job.lease_until = now + timedelta(seconds=lease_seconds)
    job.updated_at = now
    job.last_error = None
    session.add(job)
    session.flush()
    session.refresh(job)
    return job


def _retry(session: Session, job: ColdStartJob, error: str, *, max_delay_seconds: int) -> ColdStartJob:
    now = utc_now()
    delay = min(2 ** min(job.attempts, 10), max_delay_seconds)
    job.status = "pending"
    job.next_attempt_at = now + timedelta(seconds=delay)
    job.lease_until = None
    job.updated_at = now
    job.last_error = error
    session.add(job)
    session.flush()
    session.refresh(job)
    return job


def _complete(session: Session, user_id: str) -> bool:
    job = session.get(ColdStartJob, user_id)
    if job is None:
        return False
    session.delete(job)
    session.flush()
    return True


def enqueue(
    *, user_id: str, status: Literal["pending", "processing"] = "pending",
    attempts: int = 0, next_attempt_at: datetime | None = None,
    lease_until: datetime | None = None, last_error: str | None = None,
) -> ColdStartJob:
    """Idempotently enqueue a user for cold-start profile generation.

    New jobs normally need only ``user_id``. Status, attempts, schedule, lease, and
    error fields support restoration/backfill and are validated before insertion.
    """
    data = ColdStartJobCreate(**locals())
    return run(lambda session: _enqueue(session, data))


def claim(*, lease_seconds: int = 60) -> ColdStartJob | None:
    """Atomically claim the next eligible job for ``lease_seconds``, or return ``None``."""
    return run(lambda session: _claim(session, lease_seconds=lease_seconds))


def retry(
    *, user_id: str, error: str, max_delay_seconds: int = 900
) -> ColdStartJob:
    """Release a claimed job and schedule its exponential-backoff retry."""
    def operation(session):
        job = session.get(ColdStartJob, user_id)
        if job is None:
            raise RecordNotFoundError(f"Cold-start job not found: {user_id}")
        return _retry(
            session, job, error, max_delay_seconds=max_delay_seconds
        )
    return run(operation)


def complete(user_id: str) -> bool:
    """Delete a completed job; return ``False`` when it was already absent."""
    return run(lambda session: _complete(session, user_id))
