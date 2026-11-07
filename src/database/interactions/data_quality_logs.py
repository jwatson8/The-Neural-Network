"""Append-only sanitized Data Quality audit records using shared transactions."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from src.database.interactions._pagination import SearchCursor, SearchPage, search_records
from src.database.interactions._shared import create_record, list_records as list_rows, run, schema_values
from src.database.models import DataQualityLog
from src.database.schemas import DataQualityLogCreate

__all__ = ["create", "get", "list_records", "search"]


def create(
    *, source: str, severity: str, issue_type: str,
    error_details: list[dict] | None = None, log_metadata: dict | None = None,
    occurred_at: datetime | None = None, user_id: str | None = None,
    movie_id: str | None = None, event_id: UUID | None = None, request_id: UUID | None = None,
) -> DataQualityLog:
    """Commit sanitized details/metrics, with explicitly supplied optional IDs.

    Callers must not pass raw payloads, arbitrary exception messages or secrets.
    The Data Quality sink supplies only allowlisted, sanitized fields.
    """
    data = DataQualityLogCreate(
        **{**locals(), "error_details": [] if error_details is None else error_details,
           "log_metadata": {} if log_metadata is None else log_metadata}
    )
    return run(lambda session: create_record(session, DataQualityLog, schema_values(data)))


def get(log_id: UUID) -> DataQualityLog | None:
    """Return a single audit record, or None."""
    return run(lambda session: session.get(DataQualityLog, log_id))


def list_records(*, offset: int = 0, limit: int = 100) -> list[DataQualityLog]:
    """Read an offset page of audit records, with a maximum limit of 1,000."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("offset must be nonnegative and limit must be 1–1000")
    return run(lambda session: list_rows(session, DataQualityLog, offset=offset, limit=limit))


def search(
    *, start: datetime | None = None, end: datetime | None = None,
    source: str | None = None, severity: Literal["INFO", "WARNING", "ERROR"] | None = None,
    issue_type: str | None = None, user_id: str | None = None, movie_id: str | None = None,
    event_id: UUID | None = None, request_id: UUID | None = None,
    cursor: SearchCursor | None = None, limit: int = 100,
) -> SearchPage[DataQualityLog]:
    """Search newest-first by occurred_at; start inclusive, end exclusive."""
    conditions = [
        column == value
        for column, value in (
            (DataQualityLog.source, source), (DataQualityLog.severity, severity),
            (DataQualityLog.issue_type, issue_type), (DataQualityLog.user_id, user_id),
            (DataQualityLog.movie_id, movie_id), (DataQualityLog.event_id, event_id),
            (DataQualityLog.request_id, request_id),
        ) if value is not None
    ]
    return run(lambda session: search_records(
        session, DataQualityLog, timestamp_column=DataQualityLog.occurred_at,
        id_column=DataQualityLog.log_id, scope="data_quality_logs:occurred_at",
        conditions=conditions, start=start, end=end, cursor=cursor, limit=limit,
    ))
