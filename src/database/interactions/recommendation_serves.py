"""Immutable recommendation-serve audit interactions."""

import time
from datetime import datetime
from uuid import UUID

from sqlmodel import Session

from src.database.interactions._pagination import (
    SearchCursor,
    SearchPage,
    search_records,
)
from src.database.interactions._shared import (
    create_record,
    run,
    schema_values,
)
from src.database.interactions._shared import (
    list_records as list_model_records,
)
from src.database.models import RecommendationServe
from src.database.schemas import RecommendationItem, RecommendationServeCreate

__all__ = ["create", "get", "list_records", "search"]

### ----------- Helper Functions -----------


def _create(
    session: Session,
    data: RecommendationServeCreate,
    request_started_at: float | None,
) -> RecommendationServe:
    record = create_record(session, RecommendationServe, schema_values(data))
    if request_started_at is not None:
        record.serve_duration_ms = max(
            0.0, (time.perf_counter() - request_started_at) * 1000
        )
        session.add(record)
        session.flush()
        session.refresh(record)
    return record


def _get(session: Session, request_id: UUID) -> RecommendationServe | None:
    return session.get(RecommendationServe, request_id)


def _list(session: Session, *, offset: int, limit: int) -> list[RecommendationServe]:
    return list_model_records(session, RecommendationServe, offset=offset, limit=limit)


def _search(
    session: Session,
    *,
    start: datetime | None,
    end: datetime | None,
    user_id: str | None,
    model_route: str | None,
    model_version: str | None,
    cursor: SearchCursor | None,
    limit: int,
) -> SearchPage[RecommendationServe]:
    conditions = []
    for column, value in (
        (RecommendationServe.user_id, user_id),
        (RecommendationServe.model_route, model_route),
        (RecommendationServe.model_version, model_version),
    ):
        if value is not None:
            conditions.append(column == value)
    return search_records(
        session,
        RecommendationServe,
        timestamp_column=RecommendationServe.served_at,
        id_column=RecommendationServe.request_id,
        scope="recommendation_serves:served_at",
        conditions=conditions,
        start=start,
        end=end,
        cursor=cursor,
        limit=limit,
    )


### ----------- Individual Functions -----------


def create(
    *,
    user_id: str,
    served_at: datetime,
    model_route: str,
    model_version: str,
    recommendations: list[RecommendationItem | dict],
    request_received_at: datetime,
    serve_duration_ms: float,
    request_id: UUID | None = None,
    request_started_at: float | None = None,
) -> RecommendationServe:
    """Commit one served recommendation list in its original ranking order.

    ``recommendations`` contains one to twenty dictionaries/items with ``movie_id``
    and optional ``model_score``. Timing fields describe request receipt, completion,
    and elapsed milliseconds. ``request_started_at`` is an optional monotonic timer
    origin; when supplied, duration is measured through the audit insert and refresh.
    ``request_id`` is generated when omitted.
    """
    data = RecommendationServeCreate(
        user_id=user_id,
        served_at=served_at,
        model_route=model_route,
        model_version=model_version,
        recommendations=recommendations,
        request_received_at=request_received_at,
        serve_duration_ms=serve_duration_ms,
        request_id=request_id,
    )
    return run(lambda session: _create(session, data, request_started_at))


def get(request_id: UUID) -> RecommendationServe | None:
    """Return the serve audit identified by ``request_id``, or ``None``."""
    return run(lambda session: _get(session, request_id))


### ----------- Batch Functions -----------


def list_records(*, offset: int = 0, limit: int = 100) -> list[RecommendationServe]:
    """Return serve audits using zero-based ``offset`` and capped ``limit``."""
    return run(lambda session: _list(session, offset=offset, limit=limit))


def search(
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    user_id: str | None = None,
    model_route: str | None = None,
    model_version: str | None = None,
    cursor: SearchCursor | None = None,
    limit: int = 100,
) -> SearchPage[RecommendationServe]:
    """Search serve audits newest-first using exact filters and a time window."""
    return run(
        lambda session: _search(
            session,
            start=start,
            end=end,
            user_id=user_id,
            model_route=model_route,
            model_version=model_version,
            cursor=cursor,
            limit=limit,
        )
    )
