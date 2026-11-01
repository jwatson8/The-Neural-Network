"""Durable cold-start preference profile interactions."""

from datetime import datetime
from uuid import UUID

from sqlmodel import Session

from src.database.interactions import cold_start_jobs
from src.database.interactions._shared import (
    list_records as list_model_records,
)
from src.database.interactions._shared import (
    run,
    schema_values,
    upsert_record,
)
from src.database.models import ColdStartProfile, utc_now
from src.database.schemas import ColdStartProfileUpsert

__all__ = ["upsert", "get", "list_records", "complete_generation"]


def _upsert(session: Session, data: ColdStartProfileUpsert) -> ColdStartProfile:
    values = schema_values(data)
    values.setdefault("generated_at", utc_now())
    values["updated_at"] = utc_now()
    return upsert_record(
        session,
        ColdStartProfile,
        values,
        ("user_id",),
        (
            "positive",
            "negative",
            "source_description_hash",
            "llm_request_id",
            "generated_at",
            "updated_at",
        ),
    )


def _get(session: Session, user_id: str) -> ColdStartProfile | None:
    return session.get(ColdStartProfile, user_id)


def _list(session: Session, *, offset: int, limit: int) -> list[ColdStartProfile]:
    return list_model_records(session, ColdStartProfile, offset=offset, limit=limit)


def upsert(
    *,
    user_id: str,
    positive: str = "",
    negative: str = "",
    source_description_hash: str | None = None,
    llm_request_id: UUID | None = None,
    generated_at: datetime | None = None,
) -> ColdStartProfile:
    """Create or replace a user's generated preference profile.

    Positive/negative text drives cold-start ranking. The optional 64-character
    source hash detects stale descriptions, and ``llm_request_id`` links usage.
    """
    data = ColdStartProfileUpsert(**locals())
    return run(lambda session: _upsert(session, data))


def get(user_id: str) -> ColdStartProfile | None:
    """Return the user's generated cold-start profile, or ``None``."""
    return run(lambda session: _get(session, user_id))


def list_records(*, offset: int = 0, limit: int = 100) -> list[ColdStartProfile]:
    """Return profiles using zero-based ``offset`` and capped ``limit``."""
    return run(lambda session: _list(session, offset=offset, limit=limit))


def complete_generation(
    *,
    user_id: str,
    positive: str = "",
    negative: str = "",
    source_description_hash: str | None = None,
    llm_request_id: UUID | None = None,
    generated_at: datetime | None = None,
) -> ColdStartProfile:
    """Atomically save a generated profile and remove its pending queue job.

    Use this after successful LLM generation so a crash cannot leave a saved
    profile and stale job in separate transactions. Profile fields match ``upsert``.
    """
    data = ColdStartProfileUpsert(**locals())

    def operation(session):
        profile = _upsert(session, data)
        cold_start_jobs._complete(session, user_id)
        return profile

    return run(operation)
