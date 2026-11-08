"""Calculate recommendation quality in independent monitoring buckets."""

from datetime import datetime

from src.database.interactions import rating_attributions
from src.monitoring.buckets import floor_to_bucket
from src.monitoring.schemas import EvaluationWindow, MonitoringConfig, QualitySummary


def calculate_live_model_accuracy_buckets(
    windows: list[EvaluationWindow],
    config: MonitoringConfig,
) -> dict[datetime, QualitySummary]:
    """Attribute ratings in the enclosing range and group them by bucket."""
    if not windows:
        return {}
    starts = {window.start for window in windows}
    totals = {
        start: {"evaluated": 0, "attributed": 0, "positive": 0}
        for start in starts
    }
    rows = rating_attributions.search(
        start=min(starts),
        end=max(window.end for window in windows),
        attribution_hours=config.attribution_hours,
    )
    for row in rows:
        bucket_start = floor_to_bucket(row.event_time, config.bucket_minutes)
        if bucket_start not in totals:
            continue
        values = totals[bucket_start]
        values["evaluated"] += 1
        if not row.attributed:
            continue
        values["attributed"] += 1
        if row.rating >= config.positive_rating_threshold:
            values["positive"] += 1

    summaries = {}
    for start, values in totals.items():
        accuracy = None
        if values["attributed"]:
            accuracy = values["positive"] / values["attributed"]
        summaries[start] = QualitySummary(
            evaluated_rating_count=values["evaluated"],
            attributed_rating_count=values["attributed"],
            positive_rating_count=values["positive"],
            accuracy=accuracy,
        )
    return summaries


def calculate_live_model_accuracy(
    window: EvaluationWindow,
    config: MonitoringConfig,
) -> QualitySummary:
    """Compatibility wrapper for calculating one bucket."""
    return calculate_live_model_accuracy_buckets([window], config)[window.start]
