"""Calculate recommendation quality in independent monitoring buckets."""

from bisect import bisect_left
from datetime import datetime, timedelta

from src.database.interactions import rating_attributions
from src.monitoring.buckets import floor_to_bucket
from src.monitoring.schemas import (
    EvaluationWindow,
    ModelAccuracySummary,
    MonitoringConfig,
    QualitySummary,
)


def _active_models_by_window(
    windows: list[EvaluationWindow],
    attribution_hours: int,
) -> dict[datetime, set[rating_attributions.ModelIdentity]]:
    attribution_period = timedelta(hours=attribution_hours)
    serves = rating_attributions.search_model_serves(
        start=min(window.start for window in windows) - attribution_period,
        end=max(window.end for window in windows),
    )
    serve_times: dict[rating_attributions.ModelIdentity, list[datetime]] = {}
    for serve in serves:
        serve_times.setdefault(serve.model, []).append(serve.served_at)
    for times in serve_times.values():
        times.sort()

    active = {window.start: set() for window in windows}
    for window in windows:
        earliest = window.start - attribution_period
        for model, times in serve_times.items():
            first_candidate = bisect_left(times, earliest)
            if first_candidate < len(times) and times[first_candidate] < window.end:
                active[window.start].add(model)
    return active


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
    active_models = _active_models_by_window(windows, config.attribution_hours)
    model_totals = {
        start: {
            model: {"attributed": 0, "positive": 0}
            for model in active_models[start]
        }
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
        positive = row.rating >= config.positive_rating_threshold
        if positive:
            values["positive"] += 1
        for model in row.models:
            model_values = model_totals[bucket_start].setdefault(
                model, {"attributed": 0, "positive": 0}
            )
            model_values["attributed"] += 1
            if positive:
                model_values["positive"] += 1

    summaries = {}
    for start, values in totals.items():
        accuracy = None
        if values["attributed"]:
            accuracy = values["positive"] / values["attributed"]
        models = []
        for model, model_values in sorted(
            model_totals[start].items(),
            key=lambda item: (item[0].model_route, item[0].model_version),
        ):
            model_accuracy = None
            if model_values["attributed"]:
                model_accuracy = (
                    model_values["positive"] / model_values["attributed"]
                )
            models.append(
                ModelAccuracySummary(
                    model_route=model.model_route,
                    model_version=model.model_version,
                    attributed_rating_count=model_values["attributed"],
                    positive_rating_count=model_values["positive"],
                    accuracy=model_accuracy,
                )
            )
        summaries[start] = QualitySummary(
            evaluated_rating_count=values["evaluated"],
            attributed_rating_count=values["attributed"],
            positive_rating_count=values["positive"],
            accuracy=accuracy,
            models=models,
        )
    return summaries


def calculate_live_model_accuracy(
    window: EvaluationWindow,
    config: MonitoringConfig,
) -> QualitySummary:
    """Compatibility wrapper for calculating one bucket."""
    return calculate_live_model_accuracy_buckets([window], config)[window.start]
