"""Summarize successful recommendation latency into monitoring buckets."""

from datetime import datetime

from src.database.interactions import request_latency
from src.monitoring.buckets import utc_datetime
from src.monitoring.schemas import (
    EvaluationWindow,
    LatencySummary,
    MonitoringConfig,
    RouteLatencySummary,
)


def calculate_request_latency_buckets(
    windows: list[EvaluationWindow],
    config: MonitoringConfig,
) -> dict[datetime, LatencySummary]:
    """Aggregate overall and per-route latency for requested completed windows."""
    if not windows:
        return {}
    starts = {window.start for window in windows}
    rows = request_latency.aggregate(
        start=min(starts),
        end=max(window.end for window in windows),
        bucket_minutes=config.bucket_minutes,
    )
    overall = {
        start: RouteLatencySummary(model_route="all") for start in starts
    }
    routes: dict[datetime, list[RouteLatencySummary]] = {
        start: [] for start in starts
    }
    for row in rows:
        bucket_start = utc_datetime(row["bucket_start"])
        if bucket_start not in starts:
            continue
        values = {
            key: row[key]
            for key in (
                "request_count",
                "request_count_over_600ms",
                "average_ms",
                "p50_ms",
                "p95_ms",
                "p99_ms",
                "maximum_ms",
            )
        }
        if row["model_route"] is None:
            overall[bucket_start] = RouteLatencySummary(
                model_route="all", **values
            )
        else:
            routes[bucket_start].append(
                RouteLatencySummary(model_route=row["model_route"], **values)
            )
    summaries = {}
    for start in starts:
        values = overall[start].model_dump(exclude={"model_route"})
        summaries[start] = LatencySummary(
            **values,
            routes=sorted(routes[start], key=lambda summary: summary.model_route),
        )
    return summaries
