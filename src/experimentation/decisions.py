"""Immutable decision values shared by the manager and database interactions."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from src.experimentation.assignment import Variant


class DecisionReason(StrEnum):
    DISABLED = "disabled"
    NOT_FOUND = "not_found"
    INACTIVE = "inactive"
    UNSCHEDULED = "unscheduled"
    NOT_STARTED = "not_started"
    EXPIRED = "expired"
    INELIGIBLE = "ineligible"
    UNKNOWN_USER = "unknown_user"
    NOT_ENROLLED = "not_enrolled"
    ASSIGNED = "assigned"


@dataclass(frozen=True)
class ExperimentDecision:
    """An assignment is a decision, not evidence that a recommendation was served."""

    experiment_id: UUID
    reason: DecisionReason
    assignment_id: UUID | None = None
    variant: Variant | None = None
    policy: str | None = None
    version: str | None = None
    newly_assigned: bool = False

    @property
    def assigned(self) -> bool:
        return self.reason == DecisionReason.ASSIGNED

