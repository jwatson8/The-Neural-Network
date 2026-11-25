"""Kafka course-log rejection counts for completed monitoring buckets."""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from src.database.interactions import kafka_rejection_counts
from src.monitoring.buckets import utc_datetime
from src.monitoring.schemas import EvaluationWindow, MonitoringConfig, RejectionSummary

Aggregate = Callable[..., list[dict[str, Any]]]


def calculate_rejection_buckets(
    windows: list[EvaluationWindow],
    config: MonitoringConfig,
    *,
    aggregate: Aggregate | None = None,
) -> dict[datetime, RejectionSummary]:
    """Count consumed Kafka records and rejections by reason for each window.

    Buckets with no consumed records keep zero counts, which Grafana treats as
    no traffic rather than a 0% rejection rate.
    """
    if not windows:
        return {}
    starts = {window.start for window in windows}
    rows = (aggregate or kafka_rejection_counts.aggregate)(
        start=min(starts),
        end=max(window.end for window in windows),
        bucket_minutes=config.bucket_minutes,
    )
    fields = set(RejectionSummary.model_fields)
    summaries = {start: RejectionSummary() for start in starts}
    for row in rows:
        bucket_start = utc_datetime(row["bucket_start"])
        if bucket_start in starts:
            summaries[bucket_start] = RejectionSummary(
                **{key: int(value) for key, value in row.items() if key in fields}
            )
    return summaries
