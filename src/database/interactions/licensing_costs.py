"""Read interactions for licensing charges derived from watch events."""

from datetime import datetime
from uuid import UUID

from sqlmodel import Session

from src.database.interactions._pagination import (
    SearchCursor,
    SearchPage,
    search_records,
)
from src.database.interactions._shared import (
    list_records as list_model_records,
)
from src.database.interactions._shared import run
from src.database.models import LicensingCost, Movie, StreamEvent

__all__ = ["get", "list_records", "search"]


### ----------- Helper Functions -----------


def _record_watch_charge(session: Session, event: StreamEvent) -> LicensingCost:
    movie = session.get(Movie, event.movie_id)
    charge = LicensingCost(
        source_event_id=event.event_id,
        charged_at=event.event_time,
        user_id=event.user_id,
        movie_id=event.movie_id,
        license_cost=movie.license_cost,
    )
    session.add(charge)
    session.flush()
    session.refresh(charge)
    return charge


def _get(session: Session, licensing_cost_id: UUID) -> LicensingCost | None:
    return session.get(LicensingCost, licensing_cost_id)


def _list(session: Session, *, offset: int, limit: int) -> list[LicensingCost]:
    return list_model_records(session, LicensingCost, offset=offset, limit=limit)


def _search(
    session: Session,
    *,
    start: datetime | None,
    end: datetime | None,
    user_id: str | None,
    movie_id: str | None,
    cursor: SearchCursor | None,
    limit: int,
) -> SearchPage[LicensingCost]:
    conditions = []
    for column, value in (
        (LicensingCost.user_id, user_id),
        (LicensingCost.movie_id, movie_id),
    ):
        if value is not None:
            conditions.append(column == value)
    return search_records(
        session,
        LicensingCost,
        timestamp_column=LicensingCost.charged_at,
        id_column=LicensingCost.licensing_cost_id,
        scope="licensing_costs:charged_at",
        conditions=conditions,
        start=start,
        end=end,
        cursor=cursor,
        limit=limit,
    )


### ----------- Individual Functions -----------


def get(licensing_cost_id: UUID) -> LicensingCost | None:
    """Return the licensing charge identified by its UUID, or ``None``."""
    return run(lambda session: _get(session, licensing_cost_id))


### ----------- Batch Functions -----------


def list_records(*, offset: int = 0, limit: int = 100) -> list[LicensingCost]:
    """Return licensing charges using zero-based ``offset`` and capped ``limit``."""
    return run(lambda session: _list(session, offset=offset, limit=limit))


def search(
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    user_id: str | None = None,
    movie_id: str | None = None,
    cursor: SearchCursor | None = None,
    limit: int = 100,
) -> SearchPage[LicensingCost]:
    """Search licensing charges newest-first by charge time and exact filters."""
    return run(
        lambda session: _search(
            session,
            start=start,
            end=end,
            user_id=user_id,
            movie_id=movie_id,
            cursor=cursor,
            limit=limit,
        )
    )
