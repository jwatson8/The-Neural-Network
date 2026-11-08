"""Immutable stream-event interactions."""

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from sqlmodel import Session, select

from src.database.interactions._pagination import (
    SearchCursor,
    SearchPage,
    search_records,
)
from src.database.interactions._shared import (
    create_record,
    dialect_insert,
    run,
    schema_values,
)
from src.database.interactions._shared import (
    list_records as list_model_records,
)
from src.database.models import StreamEvent, User, UserMovieHistory
from src.database.schemas import StreamEventCreate

__all__ = ["create", "get", "list_records", "create_many", "search"]


### ----------- Helper Functions -----------


def _lock_watch_users(
    session: Session, events: Iterable[StreamEvent | StreamEventCreate]
) -> None:
    user_ids = sorted(
        {event.user_id for event in events if event.event_type == "watch"}
    )
    if not user_ids:
        return
    statement = (
        select(User)
        .where(User.user_id.in_(user_ids))
        .order_by(User.user_id)
        .with_for_update()
    )
    session.exec(statement).all()


def _utc_event_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _materialize_event(session: Session, event: StreamEvent) -> None:
    from src.database.interactions.user_movie_history import _apply_event_to_history

    previous_watch_at = None
    if event.event_type == "watch":
        history = session.get(UserMovieHistory, (event.user_id, event.movie_id))
        if history is not None:
            previous_watch_at = history.last_recorded_watch_at

    _apply_event_to_history(session, event)
    should_charge = event.event_type == "watch" and (
        previous_watch_at is None
        or _utc_event_time(event.event_time)
        >= _utc_event_time(previous_watch_at) + timedelta(hours=24)
    )
    if should_charge:
        from src.database.interactions.licensing_costs import _record_watch_charge

        _record_watch_charge(session, event)


def _create(session: Session, data: StreamEventCreate) -> StreamEvent:
    _lock_watch_users(session, [data])
    event = create_record(session, StreamEvent, schema_values(data))
    _materialize_event(session, event)
    return event


def _get(session: Session, event_id: UUID) -> StreamEvent | None:
    return session.get(StreamEvent, event_id)


def _list(session: Session, *, offset: int, limit: int) -> list[StreamEvent]:
    return list_model_records(session, StreamEvent, offset=offset, limit=limit)


def _search(
    session: Session,
    *,
    start: datetime | None,
    end: datetime | None,
    time_basis: Literal["event_time", "stored_at"],
    user_id: str | None,
    event_type: Literal["watch", "rating", "account_created"] | None,
    movie_id: str | None,
    cursor: SearchCursor | None,
    limit: int,
) -> SearchPage[StreamEvent]:
    timestamp_columns = {
        "event_time": StreamEvent.event_time,
        "stored_at": StreamEvent.stored_at,
    }
    if time_basis not in timestamp_columns:
        raise ValueError("time_basis must be event_time or stored_at")
    timestamp_column = timestamp_columns[time_basis]
    conditions = []
    for column, value in (
        (StreamEvent.user_id, user_id),
        (StreamEvent.event_type, event_type),
        (StreamEvent.movie_id, movie_id),
    ):
        if value is not None:
            conditions.append(column == value)
    return search_records(
        session,
        StreamEvent,
        timestamp_column=timestamp_column,
        id_column=StreamEvent.event_id,
        scope=f"stream_events:{time_basis}",
        conditions=conditions,
        start=start,
        end=end,
        cursor=cursor,
        limit=limit,
    )


def _create_many(session: Session, records: list[StreamEventCreate]) -> int:
    if not records:
        return 0
    _lock_watch_users(session, records)
    values = [record.model_dump() for record in records]
    for event in values:
        event["event_id"] = event["event_id"] or uuid4()
    statement = dialect_insert(session, StreamEvent).values(values)
    result = session.execute(
        statement.on_conflict_do_nothing(index_elements=["event_id"]).returning(
            StreamEvent.event_id
        )
    )
    inserted_ids = set(result.scalars().all())
    session.flush()
    materialized_ids = set()
    inserted_events = []
    for event in values:
        event_id = event["event_id"]
        if event_id in inserted_ids and event_id not in materialized_ids:
            inserted_events.append(StreamEvent.model_construct(**event))
            materialized_ids.add(event_id)
    inserted_events = sorted(
        inserted_events,
        key=lambda record: (record.event_time, record.event_id.int),
    )
    for event in inserted_events:
        _materialize_event(session, event)
    return len(inserted_ids)


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


def search(
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    time_basis: Literal["event_time", "stored_at"] = "event_time",
    user_id: str | None = None,
    event_type: Literal["watch", "rating", "account_created"] | None = None,
    movie_id: str | None = None,
    cursor: SearchCursor | None = None,
    limit: int = 100,
) -> SearchPage[StreamEvent]:
    """Search events newest-first by occurrence time or database storage time."""
    return run(
        lambda session: _search(
            session,
            start=start,
            end=end,
            time_basis=time_basis,
            user_id=user_id,
            event_type=event_type,
            movie_id=movie_id,
            cursor=cursor,
            limit=limit,
        )
    )
