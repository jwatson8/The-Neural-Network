"""
Determine whether rating events followed matching recommendations. This is not a table, but rather a specific suite of
interaction modules that are used for monitoring purposes.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlmodel import Session, select

from src.database.database import get_engine
from src.database.models import RecommendationServe, StreamEvent


@dataclass(frozen=True)
class RatingAttribution:
    event_time: datetime
    rating: float
    attributed: bool


POSTGRES_RATING_ATTRIBUTION = text(
    """
    SELECT rating.event_time, rating.rating,
           EXISTS (
               SELECT 1
               FROM recommendation_serves AS serve
               WHERE serve.user_id = rating.user_id
                 AND serve.served_at >= rating.event_time
                     - (:attribution_seconds * INTERVAL '1 second')
                 AND serve.served_at <= rating.event_time
                 AND EXISTS (
                     SELECT 1
                     FROM jsonb_array_elements(serve.recommendations) AS item
                     WHERE item ->> 'movie_id' = rating.movie_id
                 )
           ) AS attributed
    FROM stream_events AS rating
    WHERE rating.event_type = 'rating'
      AND rating.event_time >= :start
      AND rating.event_time < :end
    """
)


def _sqlite_rating_attributions(
    session: Session,
    start: datetime,
    end: datetime,
    attribution_hours: int,
) -> list[RatingAttribution]:
    ratings = session.exec(
        select(StreamEvent).where(
            StreamEvent.event_type == "rating",
            StreamEvent.event_time >= start,
            StreamEvent.event_time < end,
        )
    ).all()
    serves = session.exec(
        select(RecommendationServe).where(
            RecommendationServe.served_at >= start - timedelta(hours=attribution_hours),
            RecommendationServe.served_at < end,
        )
    ).all()
    results = []
    attribution_period = timedelta(hours=attribution_hours)
    for rating in ratings:
        earliest_serve = rating.event_time - attribution_period
        attributed = any(
            serve.user_id == rating.user_id
            and earliest_serve <= serve.served_at <= rating.event_time
            and any(
                item["movie_id"] == rating.movie_id for item in serve.recommendations
            )
            for serve in serves
        )
        results.append(
            RatingAttribution(
                event_time=rating.event_time,
                rating=rating.rating,
                attributed=attributed,
            )
        )
    return results


def search(
    *,
    start: datetime,
    end: datetime,
    attribution_hours: int,
) -> list[RatingAttribution]:
    """Return ratings and whether an indexed preceding serve contained the movie."""
    engine = get_engine()
    with Session(engine) as session:
        if engine.dialect.name == "sqlite":
            return _sqlite_rating_attributions(session, start, end, attribution_hours)
        rows = session.execute(
            POSTGRES_RATING_ATTRIBUTION,
            {
                "start": start,
                "end": end,
                "attribution_seconds": attribution_hours * 3600,
            },
        ).all()
        return [
            RatingAttribution(
                event_time=row.event_time,
                rating=row.rating,
                attributed=row.attributed,
            )
            for row in rows
        ]
