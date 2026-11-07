"""Materialized user/movie history interactions."""

from collections.abc import Iterable, Mapping
from datetime import datetime

from sqlmodel import Session, select

from src.database.interactions._shared import (
    list_records as list_model_records,
)
from src.database.interactions._shared import (
    run,
    schema_values,
    upsert_record,
)
from src.database.models import UserMovieHistory, utc_now
from src.database.schemas import UserMovieHistoryUpsert

__all__ = ["upsert", "get", "get_user_history", "list_records", "upsert_many"]

### ----------- Helper Functions -----------


def _upsert(session: Session, data: UserMovieHistoryUpsert) -> UserMovieHistory:
    values = schema_values(data)
    values.setdefault("stored_at", utc_now())
    return upsert_record(
        session,
        UserMovieHistory,
        values,
        ("user_id", "movie_id"),
        ("latest_rating", "stored_at"),
    )


def _get(session: Session, user_id: str, movie_id: str) -> UserMovieHistory | None:
    return session.get(UserMovieHistory, (user_id, movie_id))


def _get_user_history(session: Session, user_id: str) -> list[UserMovieHistory]:
    statement = (
        select(UserMovieHistory)
        .where(UserMovieHistory.user_id == user_id)
        .order_by(UserMovieHistory.movie_id)
    )
    return list(session.exec(statement).all())


def _list(session: Session, *, offset: int, limit: int) -> list[UserMovieHistory]:
    return list_model_records(session, UserMovieHistory, offset=offset, limit=limit)


def _upsert_many(session: Session, records: list[UserMovieHistoryUpsert]) -> int:
    for data in records:
        _upsert(session, data)
    return len(records)


### ----------- Individual Functions -----------


def upsert(
    *,
    user_id: str,
    movie_id: str,
    latest_rating: float | None = None,
    stored_at: datetime | None = None,
) -> UserMovieHistory:
    """Create or replace one user's latest state for a movie.

    ``user_id`` and ``movie_id`` form the composite key. ``latest_rating`` is
    nullable or 1–10; ``stored_at`` defaults to the current UTC storage time.
    """
    data = UserMovieHistoryUpsert(**locals())
    return run(lambda session: _upsert(session, data))


def get(user_id: str, movie_id: str) -> UserMovieHistory | None:
    """Return one user/movie history row, or ``None`` when it does not exist."""
    return run(lambda session: _get(session, user_id, movie_id))


def get_user_history(user_id: str) -> list[UserMovieHistory]:
    """Return all history rows for a user, ordered by movie identifier."""
    return run(lambda session: _get_user_history(session, user_id))


def list_records(*, offset: int = 0, limit: int = 100) -> list[UserMovieHistory]:
    """Return history rows using zero-based ``offset`` and capped ``limit``."""
    return run(lambda session: _list(session, offset=offset, limit=limit))


### ----------- Batch Functions -----------


def upsert_many(
    records: Iterable[UserMovieHistoryUpsert | Mapping[str, object]],
) -> int:
    """Insert or update a validated history batch in one transaction."""
    validated = [UserMovieHistoryUpsert.model_validate(record) for record in records]
    return run(lambda session: _upsert_many(session, validated))
