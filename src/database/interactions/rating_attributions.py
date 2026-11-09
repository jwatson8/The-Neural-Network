"""
Determine whether rating events followed matching recommendations. This is not a table, but rather a specific suite of
interaction modules that are used for monitoring purposes.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlmodel import Session, select

from src.database.database import get_engine
from src.database.models import RecommendationServe, StreamEvent


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True)
class ModelIdentity:
    model_route: str
    model_version: str


@dataclass(frozen=True)
class ModelServe:
    served_at: datetime
    model: ModelIdentity


@dataclass(frozen=True)
class RatingAttribution:
    event_time: datetime
    rating: float
    models: tuple[ModelIdentity, ...]

    @property
    def attributed(self) -> bool:
        return bool(self.models)


POSTGRES_RATING_ATTRIBUTION = text(
    """
    SELECT rating.event_id, rating.event_time, rating.rating,
           matched.model_route, matched.model_version
    FROM stream_events AS rating
    LEFT JOIN LATERAL (
        SELECT DISTINCT serve.model_route, serve.model_version
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
    ) AS matched ON TRUE
    WHERE rating.event_type = 'rating'
      AND rating.event_time >= :start
      AND rating.event_time < :end
    ORDER BY rating.event_id, matched.model_route, matched.model_version
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
        models = {
            ModelIdentity(serve.model_route, serve.model_version)
            for serve in serves
            if serve.user_id == rating.user_id
            and earliest_serve <= serve.served_at <= rating.event_time
            and any(
                item["movie_id"] == rating.movie_id for item in serve.recommendations
            )
        }
        results.append(
            RatingAttribution(
                event_time=_utc(rating.event_time),
                rating=rating.rating,
                models=tuple(
                    sorted(
                        models,
                        key=lambda item: (item.model_route, item.model_version),
                    )
                ),
            )
        )
    return results


def search(
    *,
    start: datetime,
    end: datetime,
    attribution_hours: int,
) -> list[RatingAttribution]:
    """Return ratings and each route/version that recommended the rated movie."""
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
        grouped: dict[UUID, dict] = {}
        for row in rows:
            values = grouped.setdefault(
                row.event_id,
                {"event_time": row.event_time, "rating": row.rating, "models": []},
            )
            if row.model_route is not None:
                values["models"].append(
                    ModelIdentity(row.model_route, row.model_version)
                )
        return [
            RatingAttribution(
                event_time=_utc(values["event_time"]),
                rating=values["rating"],
                models=tuple(values["models"]),
            )
            for values in grouped.values()
        ]


def search_model_serves(*, start: datetime, end: datetime) -> list[ModelServe]:
    """Return serving identities and timestamps in a bounded interval."""
    engine = get_engine()
    with Session(engine) as session:
        rows = session.exec(
            select(
                RecommendationServe.served_at,
                RecommendationServe.model_route,
                RecommendationServe.model_version,
            ).where(
                RecommendationServe.served_at >= start,
                RecommendationServe.served_at < end,
            )
        ).all()
        return [
            ModelServe(
                served_at=_utc(row.served_at),
                model=ModelIdentity(row.model_route, row.model_version),
            )
            for row in rows
        ]
