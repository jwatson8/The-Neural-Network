"""Private transaction and SQL helpers shared by table interaction modules."""

from collections.abc import Callable
from typing import Any, TypeVar

from sqlmodel import Session, SQLModel, select

from src.database.database import session_scope

Result = TypeVar("Result")


class UnsetType:
    """Marker that distinguishes an omitted update from an explicit null."""


UNSET = UnsetType()


class RecordNotFoundError(LookupError):
    """Raised when a requested update target does not exist."""


def run(operation: Callable[[Session], Result]) -> Result:
    with session_scope() as session:
        return operation(session)


def supplied(**values):
    return {key: value for key, value in values.items() if value is not UNSET}


def schema_values(schema: SQLModel) -> dict[str, object]:
    return schema.model_dump(exclude_none=True)


def dialect_insert(session: Session, model_type: type[SQLModel]):
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError(f"Unsupported insert dialect: {dialect_name}")
    return insert(model_type)


def upsert_record(
    session: Session,
    model_type: type[SQLModel],
    values: dict[str, object],
    key_fields: tuple[str, ...],
    update_fields: tuple[str, ...],
):
    statement = dialect_insert(session, model_type).values(**values)
    statement = statement.on_conflict_do_update(
        index_elements=list(key_fields),
        set_={field: getattr(statement.excluded, field) for field in update_fields},
    )
    session.execute(statement)
    session.flush()
    identity = tuple(values[field] for field in key_fields)
    record = session.get(model_type, identity[0] if len(identity) == 1 else identity)
    session.refresh(record)
    return record


def create_record(
    session: Session, model_type: type[Result], values: dict[str, Any]
) -> Result:
    record = model_type.model_validate(values)
    session.add(record)
    session.flush()
    session.refresh(record)
    return record


def update_record(session: Session, record: Result, values: dict[str, Any]) -> Result:
    record.sqlmodel_update(values)
    session.add(record)
    session.flush()
    session.refresh(record)
    return record


def list_records(
    session: Session, model_type: type[Result], *, offset: int, limit: int
) -> list[Result]:
    statement = select(model_type).offset(offset).limit(min(limit, 1000))
    return list(session.exec(statement).all())
