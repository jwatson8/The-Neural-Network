"""Replay-safe storage for raw course access-log records."""

from collections.abc import Iterable, Mapping
from datetime import datetime

from sqlmodel import Session, select

from src.database.interactions._shared import (
    dialect_insert,
    run,
    schema_values,
)
from src.database.models import CourseRequest, utc_now
from src.database.schemas import CourseRequestCreate

__all__ = ["create", "create_many", "list_records"]


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


def _create_many(session: Session, records: list[CourseRequestCreate]) -> int:
    if not records:
        return 0
    unique = {}
    for record in records:
        key = (record.topic, record.partition_id, record.offset_id)
        values = record.model_dump()
        values["received_at"] = values["received_at"] or utc_now()
        unique.setdefault(key, values)
    statement = dialect_insert(session, CourseRequest).values(list(unique.values()))
    result = session.execute(
        statement.on_conflict_do_nothing(
            index_elements=["topic", "partition_id", "offset_id"]
        ).returning(CourseRequest.topic)
    )
    session.flush()
    return len(result.scalars().all())


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


def create_many(
    records: Iterable[CourseRequestCreate | Mapping[str, object]],
) -> int:
    """Insert a request batch atomically, skipping previously stored offsets."""
    validated = [CourseRequestCreate.model_validate(record) for record in records]
    if not validated:
        return 0
    return run(lambda session: _create_many(session, validated))


def list_records(*, offset: int = 0, limit: int = 100) -> list[CourseRequest]:
    """List captured course log records with bounded pagination."""
    return run(lambda session: _list(session, offset=offset, limit=limit))
