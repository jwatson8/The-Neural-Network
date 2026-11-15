"""Validated, transaction-owned persistence for experiment_assignments; no experiment execution."""

from uuid import UUID

from sqlmodel import Session, select

from src.database.interactions._shared import create_record, run, schema_values
from src.database.models import ExperimentAssignment
from src.database.schemas import ExperimentAssignmentCreate

__all__ = ["create", "get", "list_records"]


def _create(session: Session, data: ExperimentAssignmentCreate) -> ExperimentAssignment:
    return create_record(session, ExperimentAssignment, schema_values(data))


def create(data: ExperimentAssignmentCreate) -> ExperimentAssignment:
    """Validate and commit a record; duplicates raise an integrity error."""
    validated = ExperimentAssignmentCreate.model_validate(data)
    return run(lambda session: _create(session, validated))


def get(assignment_id: UUID) -> ExperimentAssignment | None:
    """Return the record with this UUID, or None."""
    return run(lambda session: session.get(ExperimentAssignment, assignment_id))


def list_records(*, experiment_id: UUID | None = None, offset: int = 0, limit: int = 100) -> list[ExperimentAssignment]:
    """Read a bounded, deterministic page, optionally restricted to an experiment."""
    if offset < 0 or not 1 <= limit <= 1000:
        raise ValueError("offset must be non-negative and limit must be between 1 and 1000")
    statement = select(ExperimentAssignment)
    if experiment_id is not None:
        statement = statement.where(ExperimentAssignment.experiment_id == experiment_id)
    statement = statement.order_by(ExperimentAssignment.assigned_at.desc(), ExperimentAssignment.assignment_id.desc()).offset(offset).limit(limit)
    return run(lambda session: list(session.exec(statement).all()))
