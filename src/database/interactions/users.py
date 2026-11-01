"""User table interactions."""

from collections.abc import Iterable, Mapping
from datetime import datetime

from sqlmodel import Session

from src.database.interactions._shared import (
    UNSET,
    RecordNotFoundError,
    UnsetType,
    create_missing_records,
    create_record,
    run,
    schema_values,
    supplied,
    update_record,
)
from src.database.interactions._shared import (
    list_records as list_model_records,
)
from src.database.models import User
from src.database.schemas import UserCreate, UserUpdate

__all__ = ["create", "get", "update", "list_records", "create_missing_many"]


### ----------- Helper Functions -----------
def _create(session: Session, data: UserCreate) -> User:
    return create_record(session, User, schema_values(data))


def _get(session: Session, user_id: str) -> User | None:
    return session.get(User, user_id)


def _update(session: Session, user: User, data: UserUpdate) -> User:
    return update_record(session, user, data.model_dump(exclude_unset=True))


def _list(session: Session, *, offset: int, limit: int) -> list[User]:
    return list_model_records(session, User, offset=offset, limit=limit)


def _create_missing_many(session: Session, records: list[UserCreate]) -> list[str]:
    return create_missing_records(session, User, records, "user_id")


### ----------- Individual Functions -----------
def create(
    *,
    user_id: str,
    first_seen: datetime | None = None,
    age: int | None = None,
    occupation: str | None = None,
    gender: str | None = None,
    self_description_likes: str | None = None,
    self_description_dislikes: str | None = None,
) -> User:
    """Create and commit a user. All params must be supplied by name, e.g.: 'user_id = '42')

    Args:
        user_id: Stable external user identifier.
        first_seen: First observation time; the database supplies UTC now if omitted.
        age: Non-negative age, when known.
        occupation: Source occupation label, when known.
        gender: Source gender value, when known.
        self_description_likes: Free-text movie preferences.
        self_description_dislikes: Free-text movie dislikes.

    Returns:
        The committed user record, readable after this function returns.
    """
    data = UserCreate(**supplied(**locals()))
    return run(lambda session: _create(session, data))


def get(user_id: str) -> User | None:
    """Return the user database information identified by ``user_id``, or ``None`` when absent."""
    return run(lambda session: _get(session, user_id))


def update(
    user_id: str,
    *,
    age: int | None | UnsetType = UNSET,
    occupation: str | None | UnsetType = UNSET,
    gender: str | None | UnsetType = UNSET,
    self_description_likes: str | None | UnsetType = UNSET,
    self_description_dislikes: str | None | UnsetType = UNSET,
) -> User:
    """Update supplied fields; omit a field to preserve it or pass ``None`` to clear it. Params, except user_id, must be suppleid by name.
    Args:
        user_id: Stable external user identifier. String.
        first_seen: First observation time.
        age: Non-negative age, when known.
        occupation: Source occupation label, when known.
        gender: Source gender value, when known.
        self_description_likes: Free-text movie preferences.
        self_description_dislikes: Free-text movie dislikes.
    Returns:
        The updated user record, readable after this function returns.
    """
    changes = UserUpdate(
        **supplied(
            age=age,
            occupation=occupation,
            gender=gender,
            self_description_likes=self_description_likes,
            self_description_dislikes=self_description_dislikes,
        )
    )

    def operation(session):
        user = _get(session, user_id)
        if user is None:
            raise RecordNotFoundError(f"User not found: {user_id}")
        return _update(session, user, changes)

    return run(operation)


### ----------- Batch Functions -----------


def list_records(*, offset: int = 0, limit: int = 100) -> list[User]:
    """Return users using zero-based ``offset`` and a maximum ``limit`` of 1,000."""
    return run(lambda session: _list(session, offset=offset, limit=limit))


def create_missing_many(
    records: Iterable[UserCreate | Mapping[str, object]],
) -> list[str]:
    """Insert absent users atomically and return IDs left unchanged."""
    validated = [UserCreate.model_validate(record) for record in records]
    return run(lambda session: _create_missing_many(session, validated))
