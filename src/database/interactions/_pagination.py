"""Private keyset-pagination machinery for immutable interaction records."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Generic, TypeVar
from uuid import UUID

from sqlalchemy import and_, or_
from sqlmodel import Session, SQLModel, select

Record = TypeVar("Record", bound=SQLModel)


@dataclass(frozen=True)
class SearchCursor:
    """Position returned by a search page and supplied to its next request."""

    scope: str
    timestamp: datetime
    record_id: UUID


@dataclass(frozen=True)
class SearchPage(Generic[Record]):
    """One bounded search result and its optional continuation cursor."""

    records: list[Record]
    next_cursor: SearchCursor | None


def _utc_boundary(value: datetime | None, name: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return value.astimezone(UTC)


def search_records(
    session: Session,
    model_type: type[Record],
    *,
    timestamp_column,
    id_column,
    scope: str,
    conditions: list[Any],
    start: datetime | None,
    end: datetime | None,
    cursor: SearchCursor | None,
    limit: int,
) -> SearchPage[Record]:
    """Search immutable records using a timestamp/UUID keyset."""
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1,000")

    start = _utc_boundary(start, "start")
    end = _utc_boundary(end, "end")
    if start is not None and end is not None and end <= start:
        raise ValueError("end must be later than start")
    if cursor is not None and cursor.scope != scope:
        raise ValueError(f"Cursor scope must be {scope}")

    filters = list(conditions)
    if start is not None:
        filters.append(timestamp_column >= start)
    if end is not None:
        filters.append(timestamp_column < end)
    if cursor is not None:
        filters.append(
            or_(
                timestamp_column < cursor.timestamp,
                and_(
                    timestamp_column == cursor.timestamp,
                    id_column < cursor.record_id,
                ),
            )
        )

    statement = (
        select(model_type)
        .where(*filters)
        .order_by(timestamp_column.desc(), id_column.desc())
        .limit(limit + 1)
    )
    matches = list(session.exec(statement).all())
    records = matches[:limit]
    next_cursor = None
    if len(matches) > limit:
        final_record = records[-1]
        next_cursor = SearchCursor(
            scope=scope,
            timestamp=getattr(final_record, timestamp_column.key),
            record_id=getattr(final_record, id_column.key),
        )
    return SearchPage(records=records, next_cursor=next_cursor)
