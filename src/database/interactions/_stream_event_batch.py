"""Set-based history and licensing projection for new stream events."""

from datetime import UTC, timedelta

from sqlmodel import Session, select

from src.database.interactions._shared import dialect_insert
from src.database.models import LicensingCost, Movie, StreamEvent, UserMovieHistory


def _utc(value):
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def materialize_many(session: Session, events: list[StreamEvent]) -> None:
    """Apply one final history row per pair and insert qualifying watch charges."""
    history_pairs = {
        (event.user_id, event.movie_id)
        for event in events
        if event.event_type in ("watch", "rating")
    }
    history_rows = []
    if history_pairs:
        from sqlalchemy import tuple_

        history_rows = session.exec(
            select(UserMovieHistory).where(
                tuple_(
                    UserMovieHistory.user_id, UserMovieHistory.movie_id
                ).in_(history_pairs)
            )
        ).all()
    states = {
        (row.user_id, row.movie_id): {
            "last_recorded_watch_at": row.last_recorded_watch_at,
            "latest_rating": row.latest_rating,
            "last_recorded_rating_at": row.last_recorded_rating_at,
            "rating_changed": row.rating_changed,
        }
        for row in history_rows
        if (row.user_id, row.movie_id) in history_pairs
    }
    watch_movie_ids = {
        event.movie_id for event in events if event.event_type == "watch"
    }
    costs = {}
    if watch_movie_ids:
        costs = {
            movie.movie_id: movie.license_cost
            for movie in session.exec(
                select(Movie).where(Movie.movie_id.in_(watch_movie_ids))
            ).all()
        }

    charges = []
    for event in events:
        if event.event_type not in ("watch", "rating"):
            continue
        key = (event.user_id, event.movie_id)
        state = states.setdefault(key, {
            "last_recorded_watch_at": None,
            "latest_rating": None,
            "last_recorded_rating_at": None,
            "rating_changed": False,
        })
        if event.event_type == "watch":
            previous = state["last_recorded_watch_at"]
            should_charge = previous is None or (
                _utc(event.event_time) >= _utc(previous) + timedelta(hours=24)
            )
            if should_charge:
                charges.append(LicensingCost(
                    source_event_id=event.event_id,
                    charged_at=event.event_time,
                    user_id=event.user_id,
                    movie_id=event.movie_id,
                    license_cost=costs[event.movie_id],
                ))
            if previous is None or _utc(event.event_time) > _utc(previous):
                state["last_recorded_watch_at"] = event.event_time
        else:
            previous_at = state["last_recorded_rating_at"]
            if previous_at is None or _utc(event.event_time) > _utc(previous_at):
                state["rating_changed"] = (
                    previous_at is not None and state["latest_rating"] != event.rating
                )
                state["latest_rating"] = event.rating
                state["last_recorded_rating_at"] = event.event_time

    if states:
        statement = dialect_insert(session, UserMovieHistory).values([
            {"user_id": user_id, "movie_id": movie_id, **state}
            for (user_id, movie_id), state in states.items()
        ])
        excluded = statement.excluded
        session.execute(statement.on_conflict_do_update(
            index_elements=["user_id", "movie_id"],
            set_={
                "last_recorded_watch_at": excluded.last_recorded_watch_at,
                "latest_rating": excluded.latest_rating,
                "last_recorded_rating_at": excluded.last_recorded_rating_at,
                "rating_changed": excluded.rating_changed,
            },
        ))
    if charges:
        session.add_all(charges)
    session.flush()
