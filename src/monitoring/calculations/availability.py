"""Client-observed recommendation availability for completed monitoring buckets."""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from src.database.interactions import course_request_availability
from src.monitoring.buckets import utc_datetime
from src.monitoring.schemas import (
    AvailabilitySummary,
    EvaluationWindow,
    MonitoringConfig,
)

Aggregate = Callable[..., list[dict[str, Any]]]


def calculate_availability_buckets(
    windows: list[EvaluationWindow],
    config: MonitoringConfig,
    *,
    aggregate: Aggregate | None = None,
) -> dict[datetime, AvailabilitySummary]:
    """Summarize course-logged request outcomes for each requested window.

    Unlike ``recommendation_serves``, the course log records requests that
    timed out or never reached the API, so it supplies the denominator that
    successful-request telemetry cannot.
    """
    if not windows:
        return {}
    starts = {window.start for window in windows}
    rows = (aggregate or course_request_availability.aggregate)(
        start=min(starts),
        end=max(window.end for window in windows),
        bucket_minutes=config.bucket_minutes,
    )
    summaries = {start: AvailabilitySummary() for start in starts}
    for row in rows:
        bucket_start = utc_datetime(row["bucket_start"])
        if bucket_start not in starts:
            continue
        request_count = int(row["request_count"])
        success_count = int(row["success_count"])
        no_response_count = int(row["no_response_count"])
        summaries[bucket_start] = AvailabilitySummary(
            request_count=request_count,
            success_count=success_count,
            no_response_count=no_response_count,
            other_failure_count=request_count - success_count - no_response_count,
        )
    return summaries
