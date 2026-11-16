"""Validated, transaction-owned persistence for experiments; no experiment execution."""

from uuid import UUID

from sqlmodel import Session, select

from src.database.interactions._shared import create_record, run, schema_values
from src.database.models import Experiment
from src.database.schemas import ExperimentCreate

__all__ = ["create", "get", "list_records"]


def _create(session: Session, data: ExperimentCreate) -> Experiment:
    return create_record(session, Experiment, schema_values(data))


def create(data: ExperimentCreate) -> Experiment:
    """Validate and commit a record; duplicates raise an integrity error."""
    validated = ExperimentCreate.model_validate(data)
    return run(lambda session: _create(session, validated))


def get(experiment_id: UUID) -> Experiment | None:
    """Return the record with this UUID, or None."""
    return run(lambda session: session.get(Experiment, experiment_id))


def list_records(*, experiment_id: UUID | None = None, offset: int = 0, limit: int = 100) -> list[Experiment]:
    """Read a bounded, deterministic page, optionally restricted to an experiment."""
    if offset < 0 or not 1 <= limit <= 1000:
        raise ValueError("offset must be non-negative and limit must be between 1 and 1000")
    statement = select(Experiment)
    if experiment_id is not None:
        statement = statement.where(Experiment.experiment_id == experiment_id)
    statement = statement.order_by(Experiment.created_at.desc(), Experiment.experiment_id.desc()).offset(offset).limit(limit)
    return run(lambda session: list(session.exec(statement).all()))
