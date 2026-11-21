"""Database-side availability counts from course recommendation-request logs.

Each ``course_requests.raw_line`` is the course client's own record of one
request, for example::

    2026-11-21T00:24:57.205,737890,recommendation request host:8082, status 200, result: a, b, 114 ms

Status ``200`` is a success, ``0`` means the client received no HTTP response
(timeout or connection failure), and anything else -- including a line with no
recognizable status -- is another failure. Requests are bucketed by the
timestamp in the line, which the course logs in UTC, so an ingestion backlog
does not pile into one bucket; lines with an unparseable timestamp fall back to
their ``received_at`` time.
"""

import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlmodel import Session, select

from src.database.interactions._shared import run
from src.database.models import CourseRequest

__all__ = ["aggregate"]

# A request is logged before we receive it; allow for clock skew between hosts
# while still letting the received_at index bound the scan.
RECEIVED_SKEW = timedelta(minutes=5)

_TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?),")
_STATUS_RE = re.compile(r", status (\d+)")


def _requested_at(raw_line: str, received_at: datetime) -> datetime:
    match = _TIMESTAMP_RE.match(raw_line)
    if match:
        whole, _, fraction = match.group(1).partition(".")
        # Python accepts at most microseconds; the course logs nanoseconds.
        value = f"{whole}.{fraction[:6]}" if fraction else whole
        return datetime.fromisoformat(value).replace(tzinfo=UTC)
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        return received_at.replace(tzinfo=UTC)
    return received_at.astimezone(UTC)


def _portable_aggregate(
    session: Session,
    *,
    start: datetime,
    end: datetime,
    bucket_minutes: int,
) -> list[dict[str, Any]]:
    rows = session.exec(
        select(CourseRequest.received_at, CourseRequest.raw_line).where(
            CourseRequest.received_at >= start - RECEIVED_SKEW
        )
    ).all()
    bucket_seconds = bucket_minutes * 60
    buckets: dict[datetime, dict[str, int]] = defaultdict(
        lambda: {"request_count": 0, "success_count": 0, "no_response_count": 0}
    )
    for received_at, raw_line in rows:
        requested_at = _requested_at(raw_line, received_at)
        if not start <= requested_at < end:
            continue
        epoch_seconds = int(requested_at.timestamp())
        bucket_start = datetime.fromtimestamp(
            epoch_seconds - epoch_seconds % bucket_seconds, tz=UTC
        )
        status = _STATUS_RE.search(raw_line)
        counts = buckets[bucket_start]
        counts["request_count"] += 1
        counts["success_count"] += bool(status and status.group(1) == "200")
        counts["no_response_count"] += bool(status and status.group(1) == "0")
    return [
        {"bucket_start": bucket_start, **counts}
        for bucket_start, counts in sorted(buckets.items())
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
            r"""
            WITH parsed AS (
                SELECT
                    CASE
                        WHEN raw_line ~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?,'
                        THEN substring(raw_line FROM '^([^,]+),')::timestamp
                            AT TIME ZONE 'UTC'
                        ELSE received_at
                    END AS requested_at,
                    substring(raw_line FROM ', status (\d+)') AS status
                FROM course_requests
                WHERE received_at >= :received_from
            )
            SELECT
                date_bin(
                    make_interval(mins => :bucket_minutes),
                    requested_at,
                    TIMESTAMPTZ '1970-01-01 00:00:00+00'
                ) AS bucket_start,
                COUNT(*)::bigint AS request_count,
                COUNT(*) FILTER (WHERE status = '200')::bigint AS success_count,
                COUNT(*) FILTER (WHERE status = '0')::bigint AS no_response_count
            FROM parsed
            WHERE requested_at >= :start AND requested_at < :end
            GROUP BY 1
            ORDER BY 1
            """
        ),
        {
            "received_from": start - RECEIVED_SKEW,
            "start": start,
            "end": end,
            "bucket_minutes": bucket_minutes,
        },
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
    """Count course requests per bucket: total, status 200, and status 0."""
    return run(
        lambda session: _aggregate(
            session, start=start, end=end, bucket_minutes=bucket_minutes
        )
    )
