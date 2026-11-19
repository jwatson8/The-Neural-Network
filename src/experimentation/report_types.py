"""Validated reporting boundaries and explicit external completeness evidence."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID


def utc(value: datetime) -> datetime:
    """Normalize public input; never silently interpret a naive analysis boundary."""
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError('report timestamps must include a timezone')
    return value.astimezone(UTC)


def stored_utc(value: datetime) -> datetime:
    """SQLite fixtures lose tzinfo; persisted timestamps follow repository UTC conventions."""
    return value.replace(tzinfo=UTC) if value.utcoffset() is None else value.astimezone(UTC)


@dataclass(frozen=True)
class ReportWindow:
    start: datetime
    end: datetime

    def __post_init__(self):
        object.__setattr__(self, 'start', utc(self.start))
        object.__setattr__(self, 'end', utc(self.end))
        if self.end <= self.start:
            raise ValueError('report end must be later than start')


@dataclass(frozen=True)
class CoverageEvidence:
    """Operator-supplied external evidence, not a watermark inferred from empty tables.

    Attests complete source delivery through the boundary and correct, ordered
    materialization of the documented billing rule. Repository telemetry alone
    does not establish this. The reference must be a nonsecret evidence identifier.
    """
    complete_through: datetime
    reference: str

    def __post_init__(self):
        object.__setattr__(self, 'complete_through', utc(self.complete_through))
        if not isinstance(self.reference, str) or not self.reference.strip() or len(self.reference) > 500:
            raise ValueError('coverage requires a nonblank evidence reference of at most 500 characters')


@dataclass(frozen=True)
class ExperimentSnapshot:
    experiment: object
    window: ReportWindow
    as_of: datetime
    effective_start: datetime
    effective_end: datetime
    assignments: tuple = ()
    events: tuple = ()
    charges: tuple = ()
    serves: tuple = ()
    overlapping_assignments: tuple = ()
    source_counts: dict = field(default_factory=dict)
