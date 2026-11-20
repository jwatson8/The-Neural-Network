"""Database-side latency aggregation for completed monitoring windows."""

from collections import defaultdict
from datetime import UTC, datetime
from math import floor
from typing import Any

from sqlalchemy import text
from sqlmodel import Session, select

from src.database.interactions._shared import run
from src.database.models import RecommendationServe

__all__ = ["aggregate"]


def _percentile_cont(sorted_values: list[float], percentile: float) -> float:
    position = (len(sorted_values) - 1) * percentile
    lower = floor(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = position - lower
    return sorted_values[lower] + (
        sorted_values[upper] - sorted_values[lower]
    ) * fraction


def _summary(
    bucket_start: datetime,
    model_route: str | None,
    durations: list[float],
) -> dict[str, Any]:
    ordered = sorted(durations)
    return {
        "bucket_start": bucket_start,
        "model_route": model_route,
        "request_count": len(ordered),
        "request_count_over_600ms": sum(value > 600 for value in ordered),
        "average_ms": sum(ordered) / len(ordered),
        "p50_ms": _percentile_cont(ordered, 0.50),
        "p95_ms": _percentile_cont(ordered, 0.95),
        "p99_ms": _percentile_cont(ordered, 0.99),
        "maximum_ms": ordered[-1],
    }


def _portable_aggregate(
    session: Session,
    *,
    start: datetime,
    end: datetime,
    bucket_minutes: int,
) -> list[dict[str, Any]]:
    rows = session.exec(
        select(
            RecommendationServe.served_at,
            RecommendationServe.model_route,
            RecommendationServe.serve_duration_ms,
        ).where(
            RecommendationServe.served_at >= start,
            RecommendationServe.served_at < end,
        )
    ).all()
    bucket_seconds = bucket_minutes * 60
    overall: dict[datetime, list[float]] = defaultdict(list)
    routes: dict[tuple[datetime, str], list[float]] = defaultdict(list)
    for served_at, model_route, duration in rows:
        if served_at.tzinfo is None or served_at.utcoffset() is None:
            served_at = served_at.replace(tzinfo=UTC)
        else:
            served_at = served_at.astimezone(UTC)
        epoch_seconds = int(served_at.timestamp())
        bucket_start = datetime.fromtimestamp(
            epoch_seconds - epoch_seconds % bucket_seconds,
            tz=UTC,
        )
        overall[bucket_start].append(duration)
        routes[(bucket_start, model_route)].append(duration)
    return [
        *(
            _summary(bucket_start, None, durations)
            for bucket_start, durations in overall.items()
        ),
        *(
            _summary(bucket_start, model_route, durations)
            for (bucket_start, model_route), durations in routes.items()
        ),
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
            WITH bucketed AS (
                SELECT
                    date_bin(
                        make_interval(mins => :bucket_minutes),
                        served_at,
                        TIMESTAMPTZ '1970-01-01 00:00:00+00'
                    ) AS bucket_start,
                    model_route,
                    serve_duration_ms
                FROM recommendation_serves
                WHERE served_at >= :start AND served_at < :end
            ), aggregated AS (
                SELECT
                    bucket_start,
                    CASE WHEN GROUPING(model_route) = 1 THEN NULL ELSE model_route END
                        AS model_route,
                    COUNT(*)::bigint AS request_count,
                    COUNT(*) FILTER (WHERE serve_duration_ms > 600)::bigint
                        AS request_count_over_600ms,
                    AVG(serve_duration_ms)::double precision AS average_ms,
                    percentile_cont(
                        ARRAY[0.50, 0.95, 0.99]::double precision[]
                    )
                        WITHIN GROUP (ORDER BY serve_duration_ms) AS percentiles_ms,
                    MAX(serve_duration_ms)::double precision AS maximum_ms
                FROM bucketed
                GROUP BY GROUPING SETS ((bucket_start), (bucket_start, model_route))
            )
            SELECT
                bucket_start,
                model_route,
                request_count,
                request_count_over_600ms,
                average_ms,
                percentiles_ms[1] AS p50_ms,
                percentiles_ms[2] AS p95_ms,
                percentiles_ms[3] AS p99_ms,
                maximum_ms
            FROM aggregated
            ORDER BY bucket_start, model_route NULLS FIRST
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
            session,
            start=start,
            end=end,
            bucket_minutes=bucket_minutes,
        )
    return _portable_aggregate(
        session,
        start=start,
        end=end,
        bucket_minutes=bucket_minutes,
    )


def aggregate(
    *,
    start: datetime,
    end: datetime,
    bucket_minutes: int,
) -> list[dict[str, Any]]:
    """Return overall and per-route latency summaries for a bounded range."""
    return run(
        lambda session: _aggregate(
            session,
            start=start,
            end=end,
            bucket_minutes=bucket_minutes,
        )
    )
