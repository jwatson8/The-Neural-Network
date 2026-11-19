"""Bounded, consistent, read-only experiment snapshots; no catalog-price reads."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, or_, text
from sqlmodel import select

from src.database.database import session_scope
from src.database.interactions._shared import RecordNotFoundError
from src.database.models import Experiment, ExperimentAssignment, StreamEvent, LicensingCost, RecommendationServe
from src.experimentation.report_types import ExperimentSnapshot, ReportWindow, stored_utc, utc

__all__ = ['load']


def load(*, experiment_id: UUID, window_start: datetime, window_end: datetime,
         as_of: datetime | None = None, row_limit: int = 200000) -> ExperimentSnapshot:
    """Read one repeatable PostgreSQL snapshot; fail rather than silently truncate.

    Includes all assignments before effective end, watch lookback for the 24-hour
    rule, relevant charge-source events and possible other-experiment assignments.
    No event-to-serve join is invented. SQLite is supported for isolated tests.
    """
    if not isinstance(experiment_id, UUID):
        raise ValueError('experiment_id must be a UUID')
    window = ReportWindow(window_start, window_end)
    instant = utc(as_of) if as_of is not None else datetime.now(UTC)
    if window.end > instant:
        raise ValueError('report end cannot exceed the source snapshot time')
    if type(row_limit) is not int or not 1 <= row_limit <= 1000000:
        raise ValueError('row_limit must be between 1 and 1000000')
    with session_scope() as session:
        if session.get_bind().dialect.name == 'postgresql':
            session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
        experiment = session.get(Experiment, experiment_id)
        if experiment is None:
            raise RecordNotFoundError('Experiment does not exist')
        start = max(window.start, stored_utc(experiment.starts_at)) if experiment.starts_at else window.start
        end = min(window.end, stored_utc(experiment.ends_at)) if experiment.ends_at else window.end

        def read(statement):
            rows = session.exec(statement.limit(row_limit+1)).all()
            if len(rows) > row_limit:
                raise ValueError('report source row limit exceeded; use a smaller window')
            return tuple(rows)

        if end <= start:
            return ExperimentSnapshot(experiment, window, instant, start, end)
        cohort = select(ExperimentAssignment.user_id).where(
            ExperimentAssignment.experiment_id == experiment_id, ExperimentAssignment.assigned_at < end)
        assignments = read(select(ExperimentAssignment).where(
            ExperimentAssignment.experiment_id == experiment_id, ExperimentAssignment.assigned_at < end))
        if not assignments:
            return ExperimentSnapshot(experiment, window, instant, start, end)
        charge_filter = and_(LicensingCost.user_id.in_(cohort), LicensingCost.charged_at >= start,
                             LicensingCost.charged_at < end)
        source_ids = select(LicensingCost.source_event_id).where(charge_filter)
        events = read(select(StreamEvent).where(
            StreamEvent.stored_at <= instant,
            or_(and_(StreamEvent.user_id.in_(cohort), StreamEvent.event_type.in_(('watch', 'rating')),
                     StreamEvent.event_time >= start-timedelta(hours=24), StreamEvent.event_time < end),
                StreamEvent.event_id.in_(source_ids))))
        charges = read(select(LicensingCost).where(charge_filter))
        serves = read(select(RecommendationServe).where(
            RecommendationServe.user_id.in_(cohort), RecommendationServe.served_at >= start,
            RecommendationServe.served_at < end))
        overlap = read(select(ExperimentAssignment, Experiment).join(
            Experiment, Experiment.experiment_id == ExperimentAssignment.experiment_id).where(
                ExperimentAssignment.user_id.in_(cohort), ExperimentAssignment.experiment_id != experiment_id,
                ExperimentAssignment.assigned_at < end,
                or_(Experiment.starts_at.is_(None), Experiment.starts_at < end),
                or_(Experiment.ends_at.is_(None), Experiment.ends_at > start)))
        return ExperimentSnapshot(experiment, window, instant, start, end, assignments, events, charges, serves,
                                  overlap, dict(assignments=len(assignments), events=len(events), charges=len(charges), serves=len(serves)))
