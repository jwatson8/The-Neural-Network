"""Validated, transaction-owned persistence for experiment_reports; no experiment execution."""

from datetime import datetime
from uuid import UUID, NAMESPACE_URL, uuid5
import json

from sqlmodel import Session, select

from src.database.interactions._shared import create_record, dialect_insert, run, schema_values
from src.database.models import Experiment, ExperimentReport
from src.database.interactions._pagination import SearchCursor, SearchPage, search_records
from src.database.schemas import ExperimentReportCreate

__all__ = ["create", "get", "list_records", "search", "save_revision"]


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


def save_revision(data: ExperimentReportCreate) -> tuple[ExperimentReport, bool]:
    """Append an immutable input revision; identical retry returns the same UUID.

    The existing PK supplies concurrent uniqueness without a new migration.
    Data/config/coverage changes must change source_fingerprint. Other writers
    using random UUIDs are outside this idempotency contract; no rows are deleted.
    """
    validated = ExperimentReportCreate.model_validate(data)
    fingerprint = validated.results.get('source_fingerprint')
    if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(c not in '0123456789abcdef' for c in fingerprint):
        raise ValueError('save_revision requires a SHA-256 source_fingerprint')
    key = json.dumps([str(validated.experiment_id), validated.window_start.isoformat(),
                      validated.window_end.isoformat(), validated.analysis_version, fingerprint], separators=(',', ':'))
    report_id = uuid5(NAMESPACE_URL, 'experiment-report:' + key)

    def operation(session):
        values = schema_values(validated)
        values['report_id'] = report_id
        record = ExperimentReport.model_validate(values)
        inserted = session.execute(dialect_insert(session, ExperimentReport).values(**record.model_dump())
            .on_conflict_do_nothing(index_elements=['report_id']).returning(ExperimentReport.report_id)).scalar_one_or_none()
        stored = session.get(ExperimentReport, report_id)
        if stored is None or stored.results.get('source_fingerprint') != fingerprint:
            raise RuntimeError('report revision conflict; retry the complete operation')
        return stored, inserted is not None

    return run(operation)
