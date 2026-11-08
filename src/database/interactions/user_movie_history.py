"""Materialized user/movie history derived from immutable stream events."""

from sqlalchemy import case
from sqlmodel import Session, select

from src.database.interactions._shared import (
    dialect_insert,
    run,
)
from src.database.interactions._shared import (
    list_records as list_model_records,
)
from src.database.models import StreamEvent, UserMovieHistory
from src.database.schemas import StreamEventCreate

__all__ = [
    "get",
    "get_user_history",
    "list_records",
]


def _apply_event_to_history(
    session: Session, event: StreamEvent | StreamEventCreate
) -> UserMovieHistory | None:
    if event.event_type not in ("watch", "rating"):
        return None

    statement = dialect_insert(session, UserMovieHistory)
    if event.event_type == "watch":
        values = {
            "user_id": event.user_id,
            "movie_id": event.movie_id,
            "last_recorded_watch_at": event.event_time,
            "rating_changed": False,
        }
        excluded = statement.excluded
        is_newer = UserMovieHistory.last_recorded_watch_at.is_(None) | (
            excluded.last_recorded_watch_at > UserMovieHistory.last_recorded_watch_at
        )
        update_values = {
            "last_recorded_watch_at": case(
                (is_newer, excluded.last_recorded_watch_at),
                else_=UserMovieHistory.last_recorded_watch_at,
            )
        }
    else:
        values = {
            "user_id": event.user_id,
            "movie_id": event.movie_id,
            "latest_rating": event.rating,
            "last_recorded_rating_at": event.event_time,
            "rating_changed": False,
        }
        excluded = statement.excluded
        is_newer = UserMovieHistory.last_recorded_rating_at.is_(None) | (
            excluded.last_recorded_rating_at > UserMovieHistory.last_recorded_rating_at
        )
        changed = UserMovieHistory.last_recorded_rating_at.is_not(None) & (
            UserMovieHistory.latest_rating != excluded.latest_rating
        )
        update_values = {
            "latest_rating": case(
                (is_newer, excluded.latest_rating),
                else_=UserMovieHistory.latest_rating,
            ),
            "last_recorded_rating_at": case(
                (is_newer, excluded.last_recorded_rating_at),
                else_=UserMovieHistory.last_recorded_rating_at,
            ),
            "rating_changed": case(
                (is_newer, changed),
                else_=UserMovieHistory.rating_changed,
            ),
        }

    statement = statement.values(**values).on_conflict_do_update(
        index_elements=["user_id", "movie_id"],
        set_=update_values,
        where=is_newer,
    )
    session.execute(statement)
    session.flush()
    history = session.get(UserMovieHistory, (event.user_id, event.movie_id))
    session.refresh(history)
    return history


def _get(session: Session, user_id: str, movie_id: str) -> UserMovieHistory | None:
    return session.get(UserMovieHistory, (user_id, movie_id))


def _get_user_history(session: Session, user_id: str) -> list[UserMovieHistory]:
    statement = (
        select(UserMovieHistory)
        .where(UserMovieHistory.user_id == user_id)
        .order_by(UserMovieHistory.movie_id)
    )
    return list(session.exec(statement).all())


def get(user_id: str, movie_id: str) -> UserMovieHistory | None:
    """Return one user/movie history row, or ``None`` when it does not exist."""
    return run(lambda session: _get(session, user_id, movie_id))


def get_user_history(user_id: str) -> list[UserMovieHistory]:
    """Return all history rows for a user, ordered by movie identifier."""
    return run(lambda session: _get_user_history(session, user_id))


def list_records(*, offset: int = 0, limit: int = 100) -> list[UserMovieHistory]:
    """Return history rows using zero-based ``offset`` and capped ``limit``."""
    return run(
        lambda session: list_model_records(
            session, UserMovieHistory, offset=offset, limit=limit
        )
    )
