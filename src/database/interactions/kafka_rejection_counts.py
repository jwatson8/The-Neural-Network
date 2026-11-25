"""Per-bucket Kafka record and rejection counts for monitoring.

The consumer stores no per-poll record total, so consumed traffic is rebuilt
from the three places a Kafka course-log record can end up:

- ``stream_events`` (watch, rating, account) by ``stored_at``
- ``course_requests`` (recommendation requests) by ``received_at``
- ``rejected_requests`` (everything rejected) by ``received_at``

All three use the time the consumer handled the record, not the time in the
line, because rejected lines often have no parseable time. Duplicate stream
events are dropped on insert and so are not counted as traffic, and stream
events written outside the consumer (such as the initial CSV roll-up) are.
"""

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlmodel import Session, select

from src.database.interactions._shared import run
from src.database.models import CourseRequest, RejectedRequest, StreamEvent
from src.database.schemas import REJECTION_REASONS

__all__ = ["aggregate"]


def _empty_counts() -> dict[str, int]:
    return {
        "kafka_record_count": 0,
        **{f"{reason}_count": 0 for reason in REJECTION_REASONS},
    }


def _bucket_start(value: datetime, bucket_seconds: int) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    epoch_seconds = int(value.timestamp())
    return datetime.fromtimestamp(epoch_seconds - epoch_seconds % bucket_seconds, tz=UTC)


def _portable_aggregate(
    session: Session, *, start: datetime, end: datetime, bucket_minutes: int
) -> list[dict[str, Any]]:
    bucket_seconds = bucket_minutes * 60
    buckets: dict[datetime, dict[str, int]] = defaultdict(_empty_counts)
    for column in (StreamEvent.stored_at, CourseRequest.received_at):
        for seen_at in session.exec(
            select(column).where(column >= start, column < end)
        ).all():
            buckets[_bucket_start(seen_at, bucket_seconds)]["kafka_record_count"] += 1
    for received_at, reason in session.exec(
        select(RejectedRequest.received_at, RejectedRequest.reason).where(
            RejectedRequest.received_at >= start, RejectedRequest.received_at < end
        )
    ).all():
        counts = buckets[_bucket_start(received_at, bucket_seconds)]
        counts["kafka_record_count"] += 1
        counts[f"{reason}_count"] += 1
    return [
        {"bucket_start": bucket_start, **counts}
        for bucket_start, counts in sorted(buckets.items())
    ]


# Reason names are fixed identifiers from REJECTION_REASONS, never caller input.
_REASON_COLUMNS = ",\n".join(
    f"    COUNT(*) FILTER (WHERE reason = '{reason}')::bigint AS {reason}_count"
    for reason in REJECTION_REASONS
)
_POSTGRES_SQL = f"""
WITH consumed AS (
    SELECT stored_at AS seen_at, NULL::text AS reason FROM stream_events
    WHERE stored_at >= :start AND stored_at < :end
    UNION ALL
    SELECT received_at, NULL::text FROM course_requests
    WHERE received_at >= :start AND received_at < :end
    UNION ALL
    SELECT received_at, reason FROM rejected_requests
    WHERE received_at >= :start AND received_at < :end
)
SELECT
    date_bin(
        make_interval(mins => :bucket_minutes),
        seen_at,
        TIMESTAMPTZ '1970-01-01 00:00:00+00'
    ) AS bucket_start,
    COUNT(*)::bigint AS kafka_record_count,
{_REASON_COLUMNS}
FROM consumed
GROUP BY 1
ORDER BY 1
"""


def _postgres_aggregate(
    session: Session, *, start: datetime, end: datetime, bucket_minutes: int
) -> list[dict[str, Any]]:
    rows = session.execute(
        text(_POSTGRES_SQL),
        {"start": start, "end": end, "bucket_minutes": bucket_minutes},
    ).mappings()
    return [dict(row) for row in rows]


def aggregate(
    *, start: datetime, end: datetime, bucket_minutes: int
) -> list[dict[str, Any]]:
    """Count consumed Kafka records and rejections by reason per bucket.

    Returns one row per non-empty bucket with ``bucket_start``,
    ``kafka_record_count`` and one ``<reason>_count`` per rejection reason.
    """

    def query(session: Session) -> list[dict[str, Any]]:
        implementation = (
            _postgres_aggregate
            if session.get_bind().dialect.name == "postgresql"
            else _portable_aggregate
        )
        return implementation(
            session, start=start, end=end, bucket_minutes=bucket_minutes
        )

    return run(query)
