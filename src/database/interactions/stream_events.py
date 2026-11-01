"""Immutable stream-event interactions."""

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from sqlmodel import Session

from src.database.interactions._shared import (
    create_record,
    dialect_insert,
    run,
    schema_values,
)
from src.database.interactions._shared import (
    list_records as list_model_records,
)
from src.database.models import StreamEvent
from src.database.schemas import StreamEventCreate

__all__ = ["create", "get", "list_records", "create_many"]


### ----------- Helper Functions -----------


def _create(session: Session, data: StreamEventCreate) -> StreamEvent:
    return create_record(session, StreamEvent, schema_values(data))


def _get(session: Session, event_id: UUID) -> StreamEvent | None:
    return session.get(StreamEvent, event_id)


def _list(session: Session, *, offset: int, limit: int) -> list[StreamEvent]:
    return list_model_records(session, StreamEvent, offset=offset, limit=limit)


def _create_many(session: Session, records: list[StreamEventCreate]) -> int:
    if not records:
        return 0
    values = [record.model_dump() for record in records]
    for event in values:
        event["event_id"] = event["event_id"] or uuid4()
    statement = dialect_insert(session, StreamEvent).values(values)
    result = session.execute(
        statement.on_conflict_do_nothing(index_elements=["event_id"]).returning(
            StreamEvent.event_id
        )
    )
    session.flush()
    return len(result.scalars().all())


### ----------- Individual Functions -----------


def create(
    *,
    user_id: str,
    event_type: Literal["watch", "rating", "account_created"],
    event_time: datetime,
    movie_id: str | None = None,
    minute_index: int | None = None,
    rating: float | None = None,
    event_id: UUID | None = None,
) -> StreamEvent:
    """Validate and commit one immutable event.

    Args:
        user_id: Existing user identifier.
        event_type: ``watch``, ``rating``, or ``account_created``.
        event_time: Source occurrence time; timezone-naive values are treated as UTC.
        movie_id: Required for watch/rating events and absent for account creation.
        minute_index: Optional non-negative playback minute.
        rating: Required only for rating events and constrained to 1–10.
        event_id: Optional deterministic UUID for replay-safe Kafka ingestion.

    Returns:
        The committed immutable event.
    """
    data = StreamEventCreate(**locals())
    return run(lambda session: _create(session, data))


def get(event_id: UUID) -> StreamEvent | None:
    """Return the immutable event identified by ``event_id``, or ``None``."""
    return run(lambda session: _get(session, event_id))


### ----------- Batch Functions -----------


def list_records(*, offset: int = 0, limit: int = 100) -> list[StreamEvent]:
    """Return events using zero-based ``offset`` and a maximum ``limit`` of 1,000."""
    return run(lambda session: _list(session, offset=offset, limit=limit))


def create_many(records: Iterable[StreamEventCreate | Mapping[str, object]]) -> int:
    """Insert a validated event batch atomically, skipping duplicate event UUIDs."""
    validated = [StreamEventCreate.model_validate(record) for record in records]
    return run(lambda session: _create_many(session, validated))
