"""Append-only model training and test audit interactions."""

from datetime import datetime
from typing import Any, Literal
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
from src.database.models import ModelLog
from src.database.schemas import ModelTestLogCreate, ModelTrainingLogCreate

__all__ = ["create_train", "create_test", "get", "list_records", "search"]


def _create(
    session: Session, data: ModelTrainingLogCreate | ModelTestLogCreate
) -> ModelLog:
    return create_record(session, ModelLog, schema_values(data))


def _search(
    session: Session,
    *,
    start: datetime | None,
    end: datetime | None,
    event_type: Literal["train", "test"] | None,
    model_name: str | None,
    training_data_source: Literal["stream_events", "user_movie_history"] | None,
    test_type: str | None,
    cursor: SearchCursor | None,
    limit: int,
) -> SearchPage[ModelLog]:
    conditions = []
    for column, value in (
        (ModelLog.event_type, event_type),
        (ModelLog.model_name, model_name),
        (ModelLog.training_data_source, training_data_source),
        (ModelLog.test_type, test_type),
    ):
        if value is not None:
            conditions.append(column == value)
    return search_records(
        session,
        ModelLog,
        timestamp_column=ModelLog.occurred_at,
        id_column=ModelLog.log_id,
        scope="model_logs:occurred_at",
        conditions=conditions,
        start=start,
        end=end,
        cursor=cursor,
        limit=limit,
    )


def create_train(
    *,
    model_name: str,
    training_data_source: Literal["stream_events", "user_movie_history"],
    training_data_start_at: datetime,
    training_data_end_at: datetime,
    training_row_count: int,
    model_size_bytes: int | None = None,
    occurred_at: datetime | None = None,
    log_id: UUID | None = None,
) -> ModelLog:
    """Record a completed training run with an inclusive/exclusive input window."""
    data = ModelTrainingLogCreate(**locals())
    return run(lambda session: _create(session, data))


def create_test(
    *,
    model_name: str,
    test_type: str,
    test_result: dict[str, Any],
    occurred_at: datetime | None = None,
    log_id: UUID | None = None,
) -> ModelLog:
    """Record a completed named model test and its structured result."""
    data = ModelTestLogCreate(**locals())
    return run(lambda session: _create(session, data))


def get(log_id: UUID) -> ModelLog | None:
    """Return the model log identified by ``log_id``, or ``None``."""
    return run(lambda session: session.get(ModelLog, log_id))


def list_records(*, offset: int = 0, limit: int = 100) -> list[ModelLog]:
    """Return model logs using zero-based ``offset`` and capped ``limit``."""
    return run(
        lambda session: list_model_records(
            session, ModelLog, offset=offset, limit=limit
        )
    )


def search(
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    event_type: Literal["train", "test"] | None = None,
    model_name: str | None = None,
    training_data_source: Literal["stream_events", "user_movie_history"] | None = None,
    test_type: str | None = None,
    cursor: SearchCursor | None = None,
    limit: int = 100,
) -> SearchPage[ModelLog]:
    """Search model logs newest-first by time and exact event attributes."""
    return run(
        lambda session: _search(
            session,
            start=start,
            end=end,
            event_type=event_type,
            model_name=model_name,
            training_data_source=training_data_source,
            test_type=test_type,
            cursor=cursor,
            limit=limit,
        )
    )
