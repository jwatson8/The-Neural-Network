"""Rolling drift calculations for completed monitoring buckets."""

from bisect import bisect_left
from collections import Counter
from datetime import datetime, timedelta

from src.data_quality.database_reader import DatabaseReader
from src.data_quality.drift import (
    EVENT_TYPES,
    detect_event_type_drift,
    detect_rating_drift,
)
from src.monitoring.buckets import utc_datetime
from src.monitoring.schemas import DriftSummary, EvaluationWindow, MonitoringConfig


def _event_type_counts(events: list[dict]) -> dict[str, int]:
    counts = Counter(event["event_type"] for event in events)
    return {event_type: counts[event_type] for event_type in EVENT_TYPES}


def calculate_data_drift_buckets(
    windows: list[EvaluationWindow],
    config: MonitoringConfig,
    *,
    reader: DatabaseReader | None = None,
) -> dict[datetime, DriftSummary]:
    """Compare the hour before each bucket's recent hour with its recent hour.

    The enclosing event range is fetched once, then split into half-open rolling
    windows in memory so backfills do not repeat the same database reads.
    """
    if not windows:
        return {}

    drift_window = timedelta(hours=config.drift_window_hours)
    first_event_time = min(window.end for window in windows) - 2 * drift_window
    last_event_time = max(window.end for window in windows)
    event_reader = reader or DatabaseReader(page_size=config.page_size)
    events = event_reader.read_events(
        start=first_event_time,
        end=last_event_time,
        time_basis="event_time",
    )
    events.sort(key=lambda event: utc_datetime(event["event_time"]))
    event_times = [utc_datetime(event["event_time"]) for event in events]

    summaries = {}
    for window in windows:
        recent_end = window.end
        recent_start = recent_end - drift_window
        baseline_start = recent_start - drift_window
        baseline = events[
            bisect_left(event_times, baseline_start) : bisect_left(
                event_times, recent_start
            )
        ]
        recent = events[
            bisect_left(event_times, recent_start) : bisect_left(
                event_times, recent_end
            )
        ]
        baseline_ratings = [
            event for event in baseline if event["event_type"] == "rating"
        ]
        recent_ratings = [
            event for event in recent if event["event_type"] == "rating"
        ]
        rating_result = detect_rating_drift(
            baseline_ratings,
            recent_ratings,
            threshold=config.drift_threshold,
            min_samples=config.drift_min_samples,
        )
        event_type_result = detect_event_type_drift(
            baseline,
            recent,
            threshold=config.drift_threshold,
            min_samples=config.drift_min_samples,
        )
        summaries[window.start] = DriftSummary(
            rating_drift_tvd=rating_result["drift_score"],
            event_type_drift_tvd=event_type_result["drift_score"],
            rating_drift_sample_count=len(recent_ratings),
            event_type_baseline_counts=_event_type_counts(baseline),
            event_type_recent_counts=_event_type_counts(recent),
        )
    return summaries
