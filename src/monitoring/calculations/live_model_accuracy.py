"""Calculate live recommendation accuracy from attributed rating events."""

from collections import defaultdict
from datetime import datetime, timedelta

from src.database.interactions import recommendation_serves, stream_events
from src.monitoring.schemas import EvaluationWindow, MonitoringConfig, QualitySummary


def calculate_live_model_accuracy(
    window: EvaluationWindow,
    config: MonitoringConfig,
) -> QualitySummary:
    """Return rating accuracy for recommendations preceding the rating event."""
    recommendation_times: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    cursor = None
    recommendation_start = window.start - timedelta(hours=config.attribution_hours)

    while True:
        page = recommendation_serves.search(
            start=recommendation_start,
            end=window.end,
            cursor=cursor,
            limit=config.page_size,
        )
        for serve in page.records:
            for recommendation in serve.recommendations:
                recommendation_times[
                    (serve.user_id, recommendation["movie_id"])
                ].append(serve.served_at)

        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    evaluated_rating_count = 0
    attributed_rating_count = 0
    positive_rating_count = 0
    attribution_period = timedelta(hours=config.attribution_hours)
    cursor = None

    while True:
        page = stream_events.search(
            start=window.start,
            end=window.end,
            time_basis="event_time",
            event_type="rating",
            cursor=cursor,
            limit=config.page_size,
        )
        evaluated_rating_count += len(page.records)
        for rating in page.records:
            earliest_serve = rating.event_time - attribution_period
            matching_times = recommendation_times.get(
                (rating.user_id, rating.movie_id), ()
            )
            is_attributed = any(
                earliest_serve <= served_at <= rating.event_time
                for served_at in matching_times
            )
            if not is_attributed:
                continue
            attributed_rating_count += 1
            if rating.rating >= config.positive_rating_threshold:
                positive_rating_count += 1

        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    accuracy = None
    if attributed_rating_count:
        accuracy = positive_rating_count / attributed_rating_count

    return QualitySummary(
        evaluated_rating_count=evaluated_rating_count,
        attributed_rating_count=attributed_rating_count,
        positive_rating_count=positive_rating_count,
        accuracy=accuracy,
    )
