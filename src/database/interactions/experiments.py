"""Validated experiment persistence and explicit lifecycle transitions."""

from datetime import UTC, datetime
from uuid import UUID

from sqlmodel import Session, select

from src.database.interactions._shared import RecordNotFoundError, create_record, run, schema_values
from src.database.models import Experiment
from src.database.schemas import ExperimentCreate, ExperimentLifecycleUpdate

__all__ = ["create", "get", "transition", "list_records"]


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


_TRANSITIONS = {
    "draft": {"running", "aborted"},
    "running": {"paused", "completed", "aborted"},
    "paused": {"running", "completed", "aborted"},
    "completed": set(),
    "aborted": set(),
}


def _utc(value: datetime) -> datetime:
    # SQLite drops timezone metadata; public inputs must still include a timezone.
    return value.replace(tzinfo=UTC) if value.utcoffset() is None else value.astimezone(UTC)


def transition(data: ExperimentLifecycleUpdate) -> Experiment:
    """Commit a legal lifecycle transition under an exclusive experiment row lock.

    Running requires a configured start and a nonexpired window. Final states
    cannot reopen. Repeating the current status is a no-op. Policy/allocation
    settings are not changed by this operation.
    """
    validated = ExperimentLifecycleUpdate.model_validate(data)

    def operation(session: Session) -> Experiment:
        record = session.exec(select(Experiment).where(
            Experiment.experiment_id == validated.experiment_id
        ).with_for_update()).one_or_none()
        if record is None:
            raise RecordNotFoundError("Experiment does not exist")
        if record.status == validated.status:
            return record
        if validated.status not in _TRANSITIONS[record.status]:
            raise ValueError(f"Cannot transition experiment from {record.status} to {validated.status}")
        if validated.occurred_at < _utc(record.updated_at):
            raise ValueError("occurred_at cannot precede updated_at")
        if validated.status == "running":
            if record.starts_at is None:
                raise ValueError("running requires starts_at")
            if record.ends_at is not None and validated.occurred_at >= _utc(record.ends_at):
                raise ValueError("cannot run an expired experiment")
        record.status = validated.status
        record.updated_at = validated.occurred_at
        session.add(record)
        session.flush()
        session.refresh(record)
        return record

    return run(operation)
