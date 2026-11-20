"""Rolling drift calculations for completed monitoring buckets."""

from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta
from fractions import Fraction
from typing import Any

from src.data_quality.drift import EVENT_TYPES
from src.database.interactions import stream_event_counts
from src.monitoring.buckets import utc_datetime
from src.monitoring.schemas import DriftSummary, EvaluationWindow, MonitoringConfig

Aggregate = Callable[..., list[dict[str, Any]]]


def _tvd(baseline: Counter, recent: Counter, min_samples: int) -> float | None:
    """Total variation distance, or None when either side lacks samples.

    Matches ``src.data_quality.drift``'s categorical TVD on counts; database
    check constraints guarantee valid event types and ratings in [1, 10].
    """
    baseline_size = sum(baseline.values())
    recent_size = sum(recent.values())
    if min(baseline_size, recent_size) < min_samples:
        return None
    return float(
        sum(
            abs(
                Fraction(baseline[key], baseline_size)
                - Fraction(recent[key], recent_size)
            )
            for key in baseline.keys() | recent.keys()
        )
        / 2
    )


def calculate_data_drift_buckets(
    windows: list[EvaluationWindow],
    config: MonitoringConfig,
    *,
    aggregate: Aggregate | None = None,
) -> dict[datetime, DriftSummary]:
    """Compare the hour before each bucket's recent hour with its recent hour.

    The enclosing range is counted once in the database per (bucket, event
    type, rating); rolling windows are then summed from those bucket counts
    so backfills neither repeat reads nor load raw events into memory.
    """
    if not windows:
        return {}

    drift_window = timedelta(hours=config.drift_window_hours)
    bucket_size = timedelta(minutes=config.bucket_minutes)
    rows = (aggregate or stream_event_counts.aggregate)(
        start=min(window.end for window in windows) - 2 * drift_window,
        end=max(window.end for window in windows),
        bucket_minutes=config.bucket_minutes,
    )

    type_counts: dict[datetime, Counter] = defaultdict(Counter)
    rating_counts: dict[datetime, Counter] = defaultdict(Counter)
    for row in rows:
        bucket_start = utc_datetime(row["bucket_start"])
        count = int(row["event_count"])
        type_counts[bucket_start][row["event_type"]] += count
        if row["event_type"] == "rating":
            rating_counts[bucket_start][float(row["rating"])] += count

    def total(counts: dict[datetime, Counter], start: datetime, end: datetime):
        combined = Counter()
        current = start
        while current < end:
            combined.update(counts.get(current, {}))
            current += bucket_size
        return combined

    summaries = {}
    for window in windows:
        recent_end = window.end
        recent_start = recent_end - drift_window
        baseline_start = recent_start - drift_window
        baseline_types = total(type_counts, baseline_start, recent_start)
        recent_types = total(type_counts, recent_start, recent_end)
        baseline_ratings = total(rating_counts, baseline_start, recent_start)
        recent_ratings = total(rating_counts, recent_start, recent_end)
        summaries[window.start] = DriftSummary(
            rating_drift_tvd=_tvd(
                baseline_ratings, recent_ratings, config.drift_min_samples
            ),
            event_type_drift_tvd=_tvd(
                baseline_types, recent_types, config.drift_min_samples
            ),
            rating_drift_sample_count=sum(recent_ratings.values()),
            event_type_baseline_counts={
                event_type: baseline_types[event_type] for event_type in EVENT_TYPES
            },
            event_type_recent_counts={
                event_type: recent_types[event_type] for event_type in EVENT_TYPES
            },
        )
    return summaries
