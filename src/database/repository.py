"""Shared persistence helpers used by table-specific interactions."""

from typing import Any, TypeVar

from sqlmodel import Session, SQLModel, select

from src.database.models import utc_now

ModelType = TypeVar("ModelType", bound=SQLModel)


def create_record(
    session: Session, model_type: type[ModelType], values: dict[str, Any]
) -> ModelType:
    record = model_type.model_validate(values)
    session.add(record)
    session.flush()
    session.refresh(record)
    return record


def get_record(
    session: Session, model_type: type[ModelType], record_id: object
) -> ModelType | None:
    return session.get(model_type, record_id)


def update_record(
    session: Session, record: ModelType, values: dict[str, Any]
) -> ModelType:
    if hasattr(record, "updated_at"):
        values["updated_at"] = utc_now()
    record.sqlmodel_update(values)
    session.add(record)
    session.flush()
    session.refresh(record)
    return record


def list_records(
    session: Session,
    model_type: type[ModelType],
    *,
    offset: int = 0,
    limit: int = 100,
) -> list[ModelType]:
    statement = select(model_type).offset(offset).limit(min(limit, 1000))
    return list(session.exec(statement).all())
