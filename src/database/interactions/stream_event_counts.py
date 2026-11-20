"""Database-side stream-event counts for monitoring buckets."""

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlmodel import Session, select

from src.database.interactions._shared import run
from src.database.models import StreamEvent

__all__ = ["aggregate"]


def _portable_aggregate(
    session: Session,
    *,
    start: datetime,
    end: datetime,
    bucket_minutes: int,
) -> list[dict[str, Any]]:
    rows = session.exec(
        select(
            StreamEvent.event_time,
            StreamEvent.event_type,
            StreamEvent.rating,
        ).where(
            StreamEvent.event_time >= start,
            StreamEvent.event_time < end,
        )
    ).all()
    bucket_seconds = bucket_minutes * 60
    counts: Counter[tuple[datetime, str, float | None]] = Counter()
    for event_time, event_type, rating in rows:
        if event_time.tzinfo is None or event_time.utcoffset() is None:
            event_time = event_time.replace(tzinfo=UTC)
        epoch_seconds = int(event_time.timestamp())
        bucket_start = datetime.fromtimestamp(
            epoch_seconds - epoch_seconds % bucket_seconds,
            tz=UTC,
        )
        counts[(bucket_start, event_type, rating)] += 1
    return [
        {
            "bucket_start": bucket_start,
            "event_type": event_type,
            "rating": rating,
            "event_count": event_count,
        }
        for (bucket_start, event_type, rating), event_count in counts.items()
    ]


def _postgres_aggregate(
    session: Session,
    *,
    start: datetime,
    end: datetime,
    bucket_minutes: int,
) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT
                date_bin(
                    make_interval(mins => :bucket_minutes),
                    event_time,
                    TIMESTAMPTZ '1970-01-01 00:00:00+00'
                ) AS bucket_start,
                event_type,
                rating,
                COUNT(*)::bigint AS event_count
            FROM stream_events
            WHERE event_time >= :start AND event_time < :end
            GROUP BY 1, 2, 3
            ORDER BY 1, 2, 3
            """
        ),
        {"start": start, "end": end, "bucket_minutes": bucket_minutes},
    ).mappings()
    return [dict(row) for row in rows]


def _aggregate(
    session: Session,
    *,
    start: datetime,
    end: datetime,
    bucket_minutes: int,
) -> list[dict[str, Any]]:
    if session.get_bind().dialect.name == "postgresql":
        return _postgres_aggregate(
            session, start=start, end=end, bucket_minutes=bucket_minutes
        )
    return _portable_aggregate(
        session, start=start, end=end, bucket_minutes=bucket_minutes
    )


def aggregate(
    *,
    start: datetime,
    end: datetime,
    bucket_minutes: int,
) -> list[dict[str, Any]]:
    """Count events per (bucket, event type, rating) by occurrence time.

    Returns a few rows per bucket instead of every raw event, so monitoring
    reads stay small however much stream traffic the window contains.
    """
    return run(
        lambda session: _aggregate(
            session, start=start, end=end, bucket_minutes=bucket_minutes
        )
    )
