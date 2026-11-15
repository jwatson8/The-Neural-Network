"""Validated, transaction-owned persistence for experiment_reports; no experiment execution."""

from datetime import datetime
from uuid import UUID

from sqlmodel import Session, select

from src.database.interactions._shared import create_record, run, schema_values
from src.database.models import Experiment, ExperimentReport
from src.database.interactions._pagination import SearchCursor, SearchPage, search_records
from src.database.schemas import ExperimentReportCreate

__all__ = ["create", "get", "list_records", "search"]


def _create(session: Session, data: ExperimentReportCreate) -> ExperimentReport:
    return create_record(session, ExperimentReport, schema_values(data))


def create(data: ExperimentReportCreate) -> ExperimentReport:
    """Validate and commit a record; duplicates raise an integrity error."""
    validated = ExperimentReportCreate.model_validate(data)
    return run(lambda session: _create(session, validated))


def get(report_id: UUID) -> ExperimentReport | None:
    """Return the record with this UUID, or None."""
    return run(lambda session: session.get(ExperimentReport, report_id))


def list_records(*, experiment_id: UUID | None = None, offset: int = 0, limit: int = 100) -> list[ExperimentReport]:
    """Read a bounded, deterministic page, optionally restricted to an experiment."""
    if offset < 0 or not 1 <= limit <= 1000:
        raise ValueError("offset must be non-negative and limit must be between 1 and 1000")
    statement = select(ExperimentReport)
    if experiment_id is not None:
        statement = statement.where(ExperimentReport.experiment_id == experiment_id)
    statement = statement.order_by(ExperimentReport.generated_at.desc(), ExperimentReport.report_id.desc()).offset(offset).limit(limit)
    return run(lambda session: list(session.exec(statement).all()))


def search(
    *, experiment_name: str | None = None, experiment_id: UUID | None = None,
    start: datetime | None = None, end: datetime | None = None,
    cursor: SearchCursor | None = None, limit: int = 100,
) -> SearchPage[ExperimentReport]:
    """Search reports newest-first by exact experiment name/ID and generated time.

    An unknown experiment name returns an empty page. Supplying both name and ID
    requires both to match. Time bounds are UTC-aware, start inclusive/end exclusive.
    Reuse the same filters when continuing with next_cursor.
    """
    conditions = []
    if experiment_name is not None:
        if not experiment_name.strip() or len(experiment_name) > 255:
            raise ValueError("experiment_name must be nonblank and at most 255 characters")
        conditions.append(ExperimentReport.experiment_id.in_(
            select(Experiment.experiment_id).where(Experiment.name == experiment_name)
        ))
    if experiment_id is not None:
        conditions.append(ExperimentReport.experiment_id == experiment_id)
    return run(lambda session: search_records(
        session, ExperimentReport, timestamp_column=ExperimentReport.generated_at,
        id_column=ExperimentReport.report_id, scope="experiment_reports:generated_at",
        conditions=conditions, start=start, end=end, cursor=cursor, limit=limit,
    ))
