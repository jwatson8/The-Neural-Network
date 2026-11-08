"""Bounded, injectable reads using Nathan's public database search interfaces."""

import json
import logging
from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from src.data_quality.drift import (
    DEFAULT_MIN_SAMPLES,
    DEFAULT_TVD_THRESHOLD,
    DriftResult,
    detect_event_type_drift,
    detect_rating_drift,
)
from src.data_quality.quality_logging import log_drift_result

LOGGER = logging.getLogger(__name__)


class SearchPageLike(Protocol):
    records: list
    next_cursor: object | None


Search = Callable[..., SearchPageLike]


class DatabaseReadError(RuntimeError):
    """A malformed page/record prevents returning a complete snapshot."""


def _window(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    for value in (start, end):
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError("Window boundaries must be timezone-aware datetimes")
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if start >= end:
        raise ValueError("end must be later than start")
    return start, end


def _filters(**filters: str | None) -> None:
    if any(value is not None and (not isinstance(value, str) or not value.strip())
           for value in filters.values()):
        raise ValueError("Optional search filters must be nonblank strings")


class DatabaseReader:
    """Inject search callables in tests; default database imports are lazy.

    Windows are required to avoid accidental unbounded reads. No SQL, engine,
    session creation, schema normalization, or sensitive payload logging here.
    """

    def __init__(
        self, *, event_search: Search | None = None,
        serve_search: Search | None = None, usage_search: Search | None = None,
        page_size: int = 1000, logger: logging.Logger | None = None,
    ) -> None:
        if type(page_size) is not int or not 1 <= page_size <= 1000:
            raise ValueError("page_size must be an integer between 1 and 1000")
        if logger is not None and not isinstance(logger, logging.Logger):
            raise TypeError("logger must be a logging.Logger")
        for search in (event_search, serve_search, usage_search):
            if search is not None and not callable(search):
                raise TypeError("Injected searches must be callable")
        self._searches = {
            "stream_events": event_search, "recommendation_serves": serve_search,
            "llm_usage": usage_search,
        }
        self.page_size = page_size
        self.logger = LOGGER if logger is None else logger

    def _log(self, level: int, source: str, operation: str, **counts: int | bool) -> None:
        self.logger.log(level, json.dumps({
            "source": source, "issue_type": "database_read", "operation": operation,
            **counts,
        }, allow_nan=False, sort_keys=True))

    def _search(self, source: str) -> Search:
        search = self._searches[source]
        if search is None:
            from src.database.interactions import llm_usage, recommendation_serves, stream_events

            search = {
                "stream_events": stream_events.search,
                "recommendation_serves": recommendation_serves.search,
                "llm_usage": llm_usage.search,
            }[source]
        return search

    def _read(self, source: str, *, start: datetime, end: datetime, **filters) -> list[dict]:
        start, end = _window(start, end)
        cursor = None
        cursors = []
        records = []
        pages = fetched = 0
        self._log(logging.INFO, source, "read_started", records_fetched=0, records_processed=0)
        try:
            search = self._search(source)
            while True:
                page = search(start=start, end=end, cursor=cursor, limit=self.page_size, **filters)
                if not hasattr(page, "records") or not hasattr(page, "next_cursor"):
                    raise DatabaseReadError("Search did not return a complete page structure")
                if not isinstance(page.records, (list, tuple)):
                    raise DatabaseReadError("Page records must be a list or tuple")
                pages += 1
                fetched += len(page.records)
                for record in page.records:
                    if isinstance(record, BaseModel):
                        records.append(record.model_dump(mode="python"))
                    elif isinstance(record, Mapping):
                        records.append(deepcopy(dict(record)))
                    else:
                        raise DatabaseReadError("Cannot convert a database record to a mapping")
                next_cursor = page.next_cursor
                if next_cursor is not None:
                    if not page.records or next_cursor in cursors:
                        raise DatabaseReadError("Pagination did not advance")
                    cursors.append(next_cursor)
                self._log(
                    logging.INFO, source, "page_processed", pages_read=pages,
                    records_fetched=fetched, records_processed=len(records),
                    pagination_complete=next_cursor is None,
                )
                if next_cursor is None:
                    break
                cursor = next_cursor
        except SQLAlchemyError:
            self._log(logging.ERROR, source, "database_read_failed", pages_read=pages,
                      records_fetched=fetched, records_processed=len(records), pagination_complete=False)
            raise
        except DatabaseReadError:
            self._log(logging.ERROR, source, "invalid_page_or_record", pages_read=pages,
                      records_fetched=fetched, records_processed=len(records), pagination_complete=False)
            raise
        self._log(logging.INFO, source, "read_completed", pages_read=pages,
                  records_fetched=fetched, records_processed=len(records), pagination_complete=True)
        return records

    def read_events(
        self, *, start: datetime, end: datetime,
        event_type: Literal["watch", "rating", "account_created"] | None = None,
        time_basis: Literal["event_time", "stored_at"] = "event_time",
        user_id: str | None = None, movie_id: str | None = None,
    ) -> list[dict]:
        if event_type is not None and event_type not in ("watch", "rating", "account_created"):
            raise ValueError("Unsupported event_type")
        if time_basis not in ("event_time", "stored_at"):
            raise ValueError("Unsupported time_basis")
        _filters(user_id=user_id, movie_id=movie_id)
        return self._read("stream_events", start=start, end=end, event_type=event_type,
                          time_basis=time_basis, user_id=user_id, movie_id=movie_id)

    def read_watch_events(self, *, start: datetime, end: datetime, **filters) -> list[dict]:
        return self.read_events(start=start, end=end, event_type="watch", **filters)

    def read_rating_events(self, *, start: datetime, end: datetime, **filters) -> list[dict]:
        return self.read_events(start=start, end=end, event_type="rating", **filters)

    def read_recommendation_serves(
        self, *, start: datetime, end: datetime, user_id: str | None = None,
        model_route: str | None = None, model_version: str | None = None,
    ) -> list[dict]:
        _filters(user_id=user_id, model_route=model_route, model_version=model_version)
        return self._read("recommendation_serves", start=start, end=end, user_id=user_id,
                          model_route=model_route, model_version=model_version)

    def read_llm_usage(
        self, *, start: datetime, end: datetime, user_id: str | None = None,
        provider: str | None = None, model_name: str | None = None, status: str | None = None,
    ) -> list[dict]:
        _filters(user_id=user_id, provider=provider, model_name=model_name, status=status)
        return self._read("llm_usage", start=start, end=end, user_id=user_id,
                          provider=provider, model_name=model_name, status=status)


def read_event_snapshots(
    reader: DatabaseReader, *, baseline_start: datetime, baseline_end: datetime,
    recent_start: datetime, recent_end: datetime,
    event_type: Literal["watch", "rating", "account_created"] | None = None,
    time_basis: Literal["event_time", "stored_at"] = "event_time",
) -> dict[str, list[dict]]:
    """Read a historical baseline followed by a disjoint recent window."""
    baseline_start, baseline_end = _window(baseline_start, baseline_end)
    recent_start, recent_end = _window(recent_start, recent_end)
    if baseline_end > recent_start:
        raise ValueError("Baseline must end before or at the recent window start")
    return {
        "baseline": reader.read_events(start=baseline_start, end=baseline_end,
                                       event_type=event_type, time_basis=time_basis),
        "recent": reader.read_events(start=recent_start, end=recent_end,
                                     event_type=event_type, time_basis=time_basis),
    }


def detect_database_drift(
    reader: DatabaseReader, *, baseline_start: datetime, baseline_end: datetime,
    recent_start: datetime, recent_end: datetime,
    time_basis: Literal["event_time", "stored_at"] = "event_time",
    threshold: float = DEFAULT_TVD_THRESHOLD, min_samples: int = DEFAULT_MIN_SAMPLES,
) -> dict[str, DriftResult]:
    """Fetch complete rating/all-event snapshots and log both drift metrics.

    Database failures propagate, never becoming empty/no-drift snapshots.
    Pre-validation missingness requires raw ingestion snapshots, not this reader.
    """
    # Reuse the pure detector's configuration checks before any database I/O.
    detect_rating_drift([], [], threshold=threshold, min_samples=min_samples)
    windows = dict(baseline_start=baseline_start, baseline_end=baseline_end,
                   recent_start=recent_start, recent_end=recent_end, time_basis=time_basis)
    ratings = read_event_snapshots(reader, **windows, event_type="rating")
    events = read_event_snapshots(reader, **windows)
    results = {
        "rating": detect_rating_drift(**ratings, threshold=threshold, min_samples=min_samples),
        "event_type": detect_event_type_drift(**events, threshold=threshold, min_samples=min_samples),
    }
    for metric, snapshots in (("rating", ratings), ("event_type", events)):
        reader.logger.info(json.dumps({
            "source": "stream_events", "issue_type": "drift_processing",
            "operation": "comparison_completed", "metric": results[metric]["metric"],
            "records_processed": sum(len(rows) for rows in snapshots.values()),
        }, sort_keys=True))
        log_drift_result(results[metric], logger=reader.logger)
    return results
