"""Transaction-owned experiment assignment persistence and eligibility decisions."""

from datetime import UTC, datetime
from uuid import UUID

from sqlmodel import Session, select

from src.database.interactions._shared import create_record, dialect_insert, run, schema_values
from src.database.models import Experiment, ExperimentAssignment, User
from src.database.schemas import ExperimentAssignmentCreate, ExperimentDecisionRequest
from src.experimentation.assignment import assigned_variant, assignment_bucket, below_allocation
from src.experimentation.decisions import ExperimentDecision, DecisionReason

__all__ = ["create", "get", "get_for_user", "get_or_create", "decide", "list_records"]


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


def _get_for_user(session: Session, experiment_id: UUID, user_id: str) -> ExperimentAssignment | None:
    return session.exec(select(ExperimentAssignment).where(
        ExperimentAssignment.experiment_id == experiment_id,
        ExperimentAssignment.user_id == user_id,
    )).one_or_none()


def get_for_user(*, experiment_id: UUID, user_id: str) -> ExperimentAssignment | None:
    """Read the persistent assignment for an experiment/user pair, or None."""
    return run(lambda session: _get_for_user(session, experiment_id, user_id))


def _get_or_create(session: Session, data: ExperimentAssignmentCreate) -> tuple[ExperimentAssignment, bool]:
    values = ExperimentAssignment.model_validate(schema_values(data)).model_dump()
    statement = dialect_insert(session, ExperimentAssignment).values(**values)
    inserted = session.execute(statement.on_conflict_do_nothing(
        index_elements=["experiment_id", "user_id"]
    ).returning(ExperimentAssignment.assignment_id)).scalar_one_or_none()
    # Under PostgreSQL READ COMMITTED, this separate SELECT sees a competing
    # insert once ON CONFLICT has waited for its transaction to finish.
    record = _get_for_user(session, data.experiment_id, data.user_id)
    if record is None:
        raise RuntimeError("assignment disappeared; retry the complete transaction")
    return record, inserted is not None


def get_or_create(data: ExperimentAssignmentCreate) -> tuple[ExperimentAssignment, bool]:
    """Insert once or return the winning assignment unchanged, plus whether inserted.

    This is persistence only: callers must perform eligibility/lifecycle checks.
    The Experiment Manager performs those checks in the same transaction.
    Concurrent use requires PostgreSQL's default READ COMMITTED isolation.
    """
    validated = ExperimentAssignmentCreate.model_validate(data)
    return run(lambda session: _get_or_create(session, validated))


def _utc_stored(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.utcoffset() is None else value.astimezone(UTC)


def decide(data: ExperimentDecisionRequest) -> ExperimentDecision:
    """Check active eligibility and commit a sticky assignment in one transaction.

    Holds a shared experiment lock so lifecycle updates cannot interleave.
    Stored assignments are reused; ineligible requests never delete or reassign.
    """
    validated = ExperimentDecisionRequest.model_validate(data)
    experiment_id, user_id = validated.experiment_id, validated.user_id
    eligible, instant = validated.eligible, validated.now
    bucket = assignment_bucket(experiment_id, user_id, "enrollment")

    def operation(session: Session) -> ExperimentDecision:
        # SHARE permits concurrent decisions but blocks lifecycle UPDATEs
        # until the assignment transaction commits. SQLite ignores row locks.
        experiment = session.exec(select(Experiment).where(
            Experiment.experiment_id == experiment_id
        ).with_for_update(read=True)).one_or_none()
        reason = None
        if experiment is None:
            reason = DecisionReason.NOT_FOUND
        elif experiment.status != "running":
            reason = DecisionReason.INACTIVE
        elif experiment.starts_at is None:
            reason = DecisionReason.UNSCHEDULED
        elif instant < _utc_stored(experiment.starts_at):
            reason = DecisionReason.NOT_STARTED
        elif experiment.ends_at is not None and instant >= _utc_stored(experiment.ends_at):
            reason = DecisionReason.EXPIRED
        elif not eligible:
            reason = DecisionReason.INELIGIBLE
        elif session.get(User, user_id) is None:
            reason = DecisionReason.UNKNOWN_USER
        if reason is not None:
            return ExperimentDecision(experiment_id, reason)

        assignment = _get_for_user(session, experiment_id, user_id)
        created = False
        if assignment is None:
            if not below_allocation(bucket, experiment.traffic_allocation):
                return ExperimentDecision(experiment_id, DecisionReason.NOT_ENROLLED)
            assignment, created = _get_or_create(
                session, ExperimentAssignmentCreate(
                    experiment_id=experiment_id, user_id=user_id,
                    variant=assigned_variant(experiment_id, user_id, experiment.treatment_allocation),
                    assigned_at=instant,
                ),
            )
        variant = assignment.variant
        policy = experiment.control_policy if variant == "control" else experiment.treatment_policy
        version = experiment.control_version if variant == "control" else experiment.treatment_version
        return ExperimentDecision(
            experiment_id=experiment_id, reason=DecisionReason.ASSIGNED,
            assignment_id=assignment.assignment_id, variant=variant,
            policy=policy, version=version, newly_assigned=created,
        )

    return run(operation)
