"""Typed, opt-in experiment eligibility and persistent assignment decisions."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from src.database.interactions import experiment_assignments
from src.database.schemas import ExperimentDecisionRequest
from src.experimentation.decisions import DecisionReason, ExperimentDecision

__all__ = ["ExperimentManager", "ExperimentDecision", "DecisionReason"]


@dataclass(frozen=True)
class ExperimentManager:
    """Opt-in manager for one explicitly selected experiment per call.

    Disabled decisions never connect to the database. Eligibility is supplied by
    the caller; the manager does not fetch profiles or select/serve models.
    Database errors propagate so serving can choose its fallback policy.
    """

    enabled: bool = False

    def __post_init__(self):
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a boolean")

    def decide(
        self, *, experiment_id: UUID, user_id: str, eligible: bool,
        now: datetime | None = None,
    ) -> ExperimentDecision:
        """Return a typed decision after committing any new assignment.

        now is an aware evaluation timestamp, defaulting to the current UTC time.
        Only running, scheduled, eligible requests may use a stored assignment.
        """
        request = ExperimentDecisionRequest(experiment_id=experiment_id, user_id=user_id,
            eligible=eligible, now=datetime.now(UTC) if now is None else now)
        if not self.enabled:
            return ExperimentDecision(request.experiment_id, DecisionReason.DISABLED)
        return experiment_assignments.decide(request)
