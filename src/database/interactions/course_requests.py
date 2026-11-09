"""Replay-safe storage for raw course access-log records."""

from datetime import datetime

from sqlmodel import Session, select

from src.database.interactions._shared import (
    dialect_insert,
    run,
    schema_values,
)
from src.database.models import CourseRequest
from src.database.schemas import CourseRequestCreate

__all__ = ["create", "list_records"]


def _create(session: Session, data: CourseRequestCreate) -> bool:
    values = schema_values(data)
    statement = dialect_insert(session, CourseRequest).values(**values)
    result = session.execute(
        statement.on_conflict_do_nothing(
            index_elements=["topic", "partition_id", "offset_id"]
        ).returning(CourseRequest.topic)
    )
    session.flush()
    return result.scalar_one_or_none() is not None


def _list(session: Session, *, offset: int, limit: int) -> list[CourseRequest]:
    statement = (
        select(CourseRequest)
        .order_by(
            CourseRequest.received_at,
            CourseRequest.topic,
            CourseRequest.partition_id,
            CourseRequest.offset_id,
        )
        .offset(offset)
        .limit(min(limit, 1000))
    )
    return list(session.exec(statement).all())


def create(
    *,
    topic: str,
    partition_id: int,
    offset_id: int,
    raw_line: str,
    received_at: datetime | None = None,
) -> bool:
    """Store a course access-log line once; return whether this Kafka offset was new."""
    data = CourseRequestCreate(**locals())
    return run(lambda session: _create(session, data))


def list_records(*, offset: int = 0, limit: int = 100) -> list[CourseRequest]:
    """List captured course log records with bounded pagination."""
    return run(lambda session: _list(session, offset=offset, limit=limit))