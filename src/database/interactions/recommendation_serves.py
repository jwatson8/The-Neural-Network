"""Immutable recommendation-serve audit interactions."""

from datetime import datetime
from uuid import UUID

from sqlmodel import Session

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

__all__ = ["create", "get", "list_records"]

### ----------- Helper Functions -----------


def _create(session: Session, data: RecommendationServeCreate) -> RecommendationServe:
    return create_record(session, RecommendationServe, schema_values(data))


def _get(session: Session, request_id: UUID) -> RecommendationServe | None:
    return session.get(RecommendationServe, request_id)


def _list(session: Session, *, offset: int, limit: int) -> list[RecommendationServe]:
    return list_model_records(session, RecommendationServe, offset=offset, limit=limit)


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
) -> RecommendationServe:
    """Commit one served recommendation list in its original ranking order.

    ``recommendations`` contains one to twenty dictionaries/items with ``movie_id``
    and optional ``model_score``. Timing fields describe request receipt, completion,
    and elapsed milliseconds; ``request_id`` is generated when omitted.
    """
    data = RecommendationServeCreate(**locals())
    return run(lambda session: _create(session, data))


def get(request_id: UUID) -> RecommendationServe | None:
    """Return the serve audit identified by ``request_id``, or ``None``."""
    return run(lambda session: _get(session, request_id))


### ----------- Batch Functions -----------


def list_records(*, offset: int = 0, limit: int = 100) -> list[RecommendationServe]:
    """Return serve audits using zero-based ``offset`` and capped ``limit``."""
    return run(lambda session: _list(session, offset=offset, limit=limit))
