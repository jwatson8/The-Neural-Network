from datetime import UTC, datetime
from decimal import Decimal
from math import isclose

from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator

from src.data_quality.drift import EVENT_TYPES


def _normalize_utc(value: datetime, field_name: str) -> datetime:
    if value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value.astimezone(UTC)


class MonitoringConfig(BaseModel):
    """Operating settings for bounded monitoring bucket reconciliation."""

    bucket_minutes: int = Field(default=5, gt=0, le=60)
    reconciliation_hours: int = Field(default=1, gt=0)
    backfill_hours: int = Field(default=24, gt=0)
    attribution_hours: int = Field(default=24, gt=0)
    positive_rating_threshold: int = Field(default=6, ge=1, le=10)
    page_size: int = Field(default=1000, ge=1, le=1000)
    drift_window_hours: int = Field(default=1, gt=0)
    drift_threshold: float = Field(default=0.20, ge=0, le=1)
    drift_min_samples: int = Field(default=100, gt=0)

    @model_validator(mode="after")
    def check_bucket_coverage(self):
        if 60 % self.bucket_minutes:
            raise ValueError("bucket_minutes must divide evenly into one hour")
        if self.reconciliation_hours > self.backfill_hours:
            raise ValueError("reconciliation_hours must not exceed backfill_hours")
        return self


class EvaluationWindow(BaseModel):
    """Half-open UTC interval used by all monitoring queries."""

    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def normalize_boundaries(cls, value: datetime, info):
        return _normalize_utc(value, info.field_name)

    @model_validator(mode="after")
    def check_boundaries(self):
        if self.end <= self.start:
            raise ValueError("end must be later than start")
        return self


class LicensingSummary(BaseModel):
    """Aggregate licensing charges for an evaluation window."""

    charge_count: int = Field(ge=0)
    priced_charge_count: int = Field(ge=0)
    total_license_cost: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def check_priced_charge_count(self):
        if self.priced_charge_count > self.charge_count:
            raise ValueError("priced_charge_count cannot exceed charge_count")
        return self


class LLMSummary(BaseModel):
    """Aggregate LLM usage and estimated cost for an evaluation window."""

    request_count: int = Field(ge=0)
    priced_request_count: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    estimated_cost_usd: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def check_priced_request_count(self):
        if self.priced_request_count > self.request_count:
            raise ValueError("priced_request_count cannot exceed request_count")
        return self


class RouteLatencySummary(BaseModel):
    """Five-minute successful-request latency for one serving route."""

    model_route: str = Field(min_length=1, max_length=100)
    request_count: int = Field(default=0, ge=0)
    request_count_over_600ms: int = Field(default=0, ge=0)
    average_ms: float | None = Field(default=None, ge=0)
    p50_ms: float | None = Field(default=None, ge=0)
    p95_ms: float | None = Field(default=None, ge=0)
    p99_ms: float | None = Field(default=None, ge=0)
    maximum_ms: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def check_distribution(self):
        if self.request_count_over_600ms > self.request_count:
            raise ValueError("request_count_over_600ms cannot exceed request_count")
        statistics = (
            self.average_ms,
            self.p50_ms,
            self.p95_ms,
            self.p99_ms,
            self.maximum_ms,
        )
        if self.request_count == 0:
            if self.request_count_over_600ms or any(
                value is not None for value in statistics
            ):
                raise ValueError("empty latency summaries cannot contain statistics")
            return self
        if any(value is None for value in statistics):
            raise ValueError("latency statistics are required when requests exist")
        if not self.p50_ms <= self.p95_ms <= self.p99_ms <= self.maximum_ms:
            raise ValueError("latency percentiles must be ordered through maximum_ms")
        if self.average_ms > self.maximum_ms:
            raise ValueError("average_ms cannot exceed maximum_ms")
        return self


class LatencySummary(RouteLatencySummary):
    """Overall latency plus route-level summaries for one monitoring bucket."""

    model_route: str = Field(default="all", exclude=True)
    routes: list[RouteLatencySummary] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_routes(self):
        identities = [summary.model_route for summary in self.routes]
        if len(identities) != len(set(identities)):
            raise ValueError("routes must contain unique model_route values")
        if any(summary.request_count == 0 for summary in self.routes):
            raise ValueError("empty route latency summaries must be omitted")
        if sum(summary.request_count for summary in self.routes) != self.request_count:
            raise ValueError("route request counts must equal the overall request count")
        if (
            sum(summary.request_count_over_600ms for summary in self.routes)
            != self.request_count_over_600ms
        ):
            raise ValueError("route slow-request counts must equal the overall count")
        return self


class ModelAccuracySummary(BaseModel):
    """Recommendation accuracy for one serving route and model version."""

    model_route: str = Field(min_length=1, max_length=100)
    model_version: str = Field(min_length=1, max_length=255)
    attributed_rating_count: int = Field(ge=0)
    positive_rating_count: int = Field(ge=0)
    accuracy: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def check_accuracy(self):
        if self.positive_rating_count > self.attributed_rating_count:
            raise ValueError(
                "positive_rating_count cannot exceed attributed_rating_count"
            )
        if self.attributed_rating_count == 0:
            if self.accuracy is not None:
                raise ValueError("accuracy must be None when no ratings are attributed")
            return self
        if self.accuracy is None:
            raise ValueError("accuracy is required when ratings are attributed")
        expected_accuracy = self.positive_rating_count / self.attributed_rating_count
        if not isclose(self.accuracy, expected_accuracy, rel_tol=0, abs_tol=1e-12):
            raise ValueError(
                "accuracy must equal positive_rating_count / attributed_rating_count"
            )
        return self


class QualitySummary(BaseModel):
    """Aggregate and per-model quality based on attributed rating events."""

    evaluated_rating_count: int = Field(ge=0)
    attributed_rating_count: int = Field(ge=0)
    positive_rating_count: int = Field(ge=0)
    accuracy: float | None = Field(default=None, ge=0, le=1)
    models: list[ModelAccuracySummary] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_accuracy(self):
        identities = [
            (summary.model_route, summary.model_version) for summary in self.models
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("models must contain unique route and version pairs")
        if self.attributed_rating_count > self.evaluated_rating_count:
            raise ValueError(
                "attributed_rating_count cannot exceed evaluated_rating_count"
            )
        if self.positive_rating_count > self.attributed_rating_count:
            raise ValueError(
                "positive_rating_count cannot exceed attributed_rating_count"
            )
        if self.attributed_rating_count == 0:
            if self.accuracy is not None:
                raise ValueError("accuracy must be None when no ratings are attributed")
            return self
        if self.accuracy is None:
            raise ValueError("accuracy is required when ratings are attributed")
        expected_accuracy = self.positive_rating_count / self.attributed_rating_count
        if not isclose(self.accuracy, expected_accuracy, rel_tol=0, abs_tol=1e-12):
            raise ValueError(
                "accuracy must equal positive_rating_count / attributed_rating_count"
            )
        return self


def _empty_event_type_counts() -> dict[str, int]:
    return {event_type: 0 for event_type in EVENT_TYPES}


class DriftSummary(BaseModel):
    """Rating and event-type drift for adjacent rolling windows."""

    rating_drift_tvd: float | None = Field(default=None, ge=0, le=1)
    event_type_drift_tvd: float | None = Field(default=None, ge=0, le=1)
    rating_drift_sample_count: int = Field(default=0, ge=0)
    event_type_baseline_counts: dict[str, StrictInt] = Field(
        default_factory=_empty_event_type_counts
    )
    event_type_recent_counts: dict[str, StrictInt] = Field(
        default_factory=_empty_event_type_counts
    )

    @field_validator("event_type_baseline_counts", "event_type_recent_counts")
    @classmethod
    def check_event_type_counts(cls, counts: dict[str, int]):
        if set(counts) != set(EVENT_TYPES):
            raise ValueError("event type counts must contain every supported event type")
        if any(isinstance(count, bool) or count < 0 for count in counts.values()):
            raise ValueError("event type counts must be nonnegative integers")
        return {event_type: counts[event_type] for event_type in EVENT_TYPES}


class AvailabilitySummary(BaseModel):
    """Client-observed request outcomes from the course request log.

    ``no_response_count`` is course status 0 (timeout or connection failure);
    ``other_failure_count`` is any other non-200 or unrecognized status.
    """

    request_count: int = Field(default=0, ge=0)
    success_count: int = Field(default=0, ge=0)
    no_response_count: int = Field(default=0, ge=0)
    other_failure_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def check_outcomes(self):
        outcomes = (
            self.success_count + self.no_response_count + self.other_failure_count
        )
        if outcomes != self.request_count:
            raise ValueError("request outcomes must sum to request_count")
        return self


class MonitorResult(BaseModel):
    """Complete monitoring payload for one evaluation window."""

    generated_at: datetime
    window: EvaluationWindow
    licensing: LicensingSummary
    llm: LLMSummary
    quality: QualitySummary
    latency: LatencySummary = Field(default_factory=LatencySummary)
    drift: DriftSummary = Field(default_factory=DriftSummary)
    availability: AvailabilitySummary = Field(default_factory=AvailabilitySummary)

    @field_validator("generated_at")
    @classmethod
    def normalize_generated_at(cls, value: datetime):
        return _normalize_utc(value, "generated_at")
