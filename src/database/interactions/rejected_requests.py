"""Replay-safe records of Kafka course-log lines the consumer rejected.

One row per rejected Kafka record, keyed by its topic/partition/offset, so
re-processing a poll after a crash (before ``consumer.commit()``) adds nothing.
Every rejection is kept, including lines too malformed to identify, together
with the original line (truncated to 1 KiB) and the user ID exactly as sent, so
format or schema changes upstream can be diagnosed after the fact.

``event_id`` is the same ``uuid5`` the consumer derives for stream events from
the Kafka position, so a row lines up with ``data_quality_logs.event_id``.

Intended use from the consumer, once per poll and before committing offsets::

    from src.database.interactions import rejected_requests

    rejected_requests.create_many([
        {
            "topic": record.topic,
            "partition_id": record.partition,
            "offset_id": record.offset,
            "raw_line": record.value,          # bytes or str
            "reason": "invalid_user_id",
            "event_kind": "watch",
            "user_id": user_id,                # as sent, even if invalid
            "event_time": parsed_time,         # None when unparseable
            "error_details": errors,           # [{"loc": ..., "type": ..., "msg": ...}]
        },
        ...
    ])

Database failures raise, like the other interaction modules, so the consumer
can retry the poll without committing.
"""

from collections.abc import Iterable, Mapping
from datetime import datetime
from uuid import UUID

from sqlmodel import Session, select

from src.database.interactions._pagination import (
    SearchCursor,
    SearchPage,
    search_records,
)
from src.database.interactions._shared import dialect_insert, run
from src.database.models import RejectedRequest, utc_now
from src.database.schemas import RejectedRequestCreate

__all__ = ["create", "create_many", "get", "list_records", "search"]

# Twelve columns per row keeps each statement well under PostgreSQL's
# 65,535 bind-parameter limit.
_INSERT_CHUNK = 1000

### ----------- Helper Functions -----------


def _values(record: RejectedRequestCreate) -> dict:
    values = record.model_dump()
    values["event_id"] = record.event_id
    values["received_at"] = values["received_at"] or utc_now()
    return values


def _insert(session: Session, records: list[RejectedRequestCreate]) -> int:
    unique: dict[UUID, dict] = {}
    for record in records:
        unique.setdefault(record.event_id, _values(record))
    rows = list(unique.values())
    inserted = 0
    for start in range(0, len(rows), _INSERT_CHUNK):
        statement = dialect_insert(session, RejectedRequest).values(
            rows[start : start + _INSERT_CHUNK]
        )
        result = session.execute(
            statement.on_conflict_do_nothing(index_elements=["event_id"]).returning(
                RejectedRequest.event_id
            )
        )
        inserted += len(result.scalars().all())
    session.flush()
    return inserted


def _list(session: Session, *, offset: int, limit: int) -> list[RejectedRequest]:
    statement = (
        select(RejectedRequest)
        .order_by(
            RejectedRequest.received_at,
            RejectedRequest.topic,
            RejectedRequest.partition_id,
            RejectedRequest.offset_id,
        )
        .offset(offset)
        .limit(min(limit, 1000))
    )
    return list(session.exec(statement).all())


def _search(
    session: Session,
    *,
    start: datetime | None,
    end: datetime | None,
    topic: str | None,
    event_kind: str | None,
    reason: str | None,
    user_id: str | None,
    cursor: SearchCursor | None,
    limit: int,
) -> SearchPage[RejectedRequest]:
    conditions = [
        column == value
        for column, value in (
            (RejectedRequest.topic, topic),
            (RejectedRequest.event_kind, event_kind),
            (RejectedRequest.reason, reason),
            (RejectedRequest.user_id, user_id),
        )
        if value is not None
    ]
    return search_records(
        session,
        RejectedRequest,
        timestamp_column=RejectedRequest.received_at,
        id_column=RejectedRequest.event_id,
        scope="rejected_requests:received_at",
        conditions=conditions,
        start=start,
        end=end,
        cursor=cursor,
        limit=limit,
    )


### ----------- Individual Functions -----------


def create(
    *,
    topic: str,
    partition_id: int,
    offset_id: int,
    raw_line: str | bytes,
    reason: str,
    event_kind: str = "unknown",
    user_id: str | None = None,
    event_time: datetime | None = None,
    error_details: list[Mapping] | None = None,
    received_at: datetime | None = None,
) -> bool:
    """Store one rejected Kafka record; return whether this offset was new.

    ``reason``: ``invalid_encoding``, ``malformed_line`` (not ``time,user,event``),
    ``unrecognized_event``, ``invalid_user_id``, ``invalid_event_time``,
    ``invalid_field`` (schema validation failed) or ``unknown_movie``.
    ``event_kind``: ``account``, ``watch``, ``rating``, ``recommendation``, or
    ``unknown`` when the line could not be classified.
    ``raw_line``: the record value as text or bytes; bad UTF-8 is escaped and
    lines over 1 KiB are truncated (``raw_line_truncated`` is then true).
    ``user_id``: the user field exactly as sent, even when invalid.
    ``event_time``/``received_at``: timezone-aware; ``received_at`` defaults to now.
    ``error_details``: pydantic-style errors (``loc``, ``type``, optional ``msg``).
    """
    data = RejectedRequestCreate(**locals())
    return run(lambda session: _insert(session, [data])) == 1


def get(event_id: UUID) -> RejectedRequest | None:
    """Return the rejection with this Kafka-derived ``event_id``, or ``None``."""
    return run(lambda session: session.get(RejectedRequest, event_id))


### ----------- Batch Functions -----------


def create_many(
    records: Iterable[RejectedRequestCreate | Mapping[str, object]],
) -> int:
    """Insert one poll's rejections in one transaction; return the new-row count.

    Each record takes the same keyword fields as ``create``. The whole batch is
    validated before anything is written. Offsets already stored, or repeated
    within the batch, are skipped, so replaying an uncommitted poll is safe.
    """
    validated = [RejectedRequestCreate.model_validate(record) for record in records]
    if not validated:
        return 0
    return run(lambda session: _insert(session, validated))


def list_records(*, offset: int = 0, limit: int = 100) -> list[RejectedRequest]:
    """List rejections oldest-first with zero-based ``offset`` and capped ``limit``."""
    return run(lambda session: _list(session, offset=offset, limit=limit))


def search(
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    topic: str | None = None,
    event_kind: str | None = None,
    reason: str | None = None,
    user_id: str | None = None,
    cursor: SearchCursor | None = None,
    limit: int = 100,
) -> SearchPage[RejectedRequest]:
    """Search rejections newest-first by ``received_at`` and exact filters.

    ``start`` is inclusive and ``end`` exclusive; both must include a timezone.
    Pass the returned ``next_cursor`` with the same filters to read the next page.
    """
    return run(
        lambda session: _search(
            session,
            start=start,
            end=end,
            topic=topic,
            event_kind=event_kind,
            reason=reason,
            user_id=user_id,
            cursor=cursor,
            limit=limit,
        )
    )
