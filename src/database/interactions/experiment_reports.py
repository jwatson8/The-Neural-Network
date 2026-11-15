"""Validated, transaction-owned persistence for experiment_reports; no experiment execution."""

from uuid import UUID

from sqlmodel import Session, select

from src.database.interactions._shared import create_record, run, schema_values
from src.database.models import ExperimentReport
from src.database.schemas import ExperimentReportCreate

__all__ = ["create", "get", "list_records"]


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
