from datetime import UTC, datetime
from decimal import Decimal
from math import isclose

from pydantic import BaseModel, Field, field_validator, model_validator


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


class QualitySummary(BaseModel):
    """Recommendation accuracy based on attributed rating events."""

    evaluated_rating_count: int = Field(ge=0)
    attributed_rating_count: int = Field(ge=0)
    positive_rating_count: int = Field(ge=0)
    accuracy: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def check_accuracy(self):
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


class MonitorResult(BaseModel):
    """Complete monitoring payload for one evaluation window."""

    generated_at: datetime
    window: EvaluationWindow
    licensing: LicensingSummary
    llm: LLMSummary
    quality: QualitySummary

    @field_validator("generated_at")
    @classmethod
    def normalize_generated_at(cls, value: datetime):
        return _normalize_utc(value, "generated_at")
