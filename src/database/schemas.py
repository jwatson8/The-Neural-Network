"""Validated input objects accepted by the database interaction layer."""

import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from math import isnan
from typing import Any, Literal, get_args
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import (
    ConfigDict,
    FiniteFloat,
    StrictBool,
    field_validator,
    model_validator,
)
from sqlmodel import Field, SQLModel


class DatabaseSchema(SQLModel):
    model_config = ConfigDict(extra="forbid")


class DataQualityLogCreate(DatabaseSchema):
    occurred_at: datetime | None = None
    source: Literal[
        "kafka",
        "user_api",
        "movie_api",
        "recommendation_api",
        "drift",
        "monitoring",
    ]
    severity: Literal["INFO", "WARNING", "ERROR"]
    issue_type: Literal[
        "validation_failure",
        "drift_result",
        "monitoring_run_failure",
    ]
    error_details: list[dict] = Field(default_factory=list)
    log_metadata: dict = Field(default_factory=dict)
    user_id: str | None = Field(default=None, min_length=1, max_length=255)
    movie_id: str | None = Field(default=None, min_length=1, max_length=255)
    event_id: UUID | None = None
    request_id: UUID | None = None

    @model_validator(mode="after")
    def normalize_log_time(self):
        if self.occurred_at is not None:
            if self.occurred_at.utcoffset() is None:
                raise ValueError("occurred_at must include a timezone")
            self.occurred_at = self.occurred_at.astimezone(UTC)
        return self


class ModelLogCreateBase(DatabaseSchema):
    log_id: UUID | None = None
    model_name: str = Field(min_length=1, max_length=255)
    occurred_at: datetime | None = None

    @model_validator(mode="after")
    def normalize_occurred_at(self):
        if self.occurred_at is not None:
            if self.occurred_at.utcoffset() is None:
                raise ValueError("occurred_at must include a timezone")
            self.occurred_at = self.occurred_at.astimezone(UTC)
        return self


class ModelTrainingLogCreate(ModelLogCreateBase):
    event_type: Literal["train"] = "train"
    training_data_source: Literal["stream_events", "user_movie_history"]
    training_data_start_at: datetime
    training_data_end_at: datetime
    training_row_count: int = Field(ge=0)
    model_size_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_training_window(self):
        for field_name in ("training_data_start_at", "training_data_end_at"):
            value = getattr(self, field_name)
            if value.utcoffset() is None:
                raise ValueError(f"{field_name} must include a timezone")
            setattr(self, field_name, value.astimezone(UTC))
        if self.training_data_end_at <= self.training_data_start_at:
            raise ValueError(
                "training_data_end_at must be later than training_data_start_at"
            )
        return self


class ModelTestLogCreate(ModelLogCreateBase):
    event_type: Literal["test"] = "test"
    test_type: str = Field(min_length=1, max_length=100)
    test_result: dict[str, Any] = Field(min_length=1)


class UserCreate(DatabaseSchema):
    user_id: str = Field(min_length=1, max_length=255)
    first_seen: datetime | None = None
    age: int | None = Field(default=None, ge=0)
    occupation: str | None = Field(default=None, max_length=255)
    gender: str | None = Field(default=None, max_length=50)
    self_description_likes: str | None = None
    self_description_dislikes: str | None = None


class UserUpdate(DatabaseSchema):
    age: int | None = Field(default=None, ge=0)
    occupation: str | None = Field(default=None, max_length=255)
    gender: str | None = Field(default=None, max_length=50)
    self_description_likes: str | None = None
    self_description_dislikes: str | None = None


class MovieCreate(DatabaseSchema):
    movie_id: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=500)
    genres: str | None = Field(default=None, max_length=1000)
    release_date: date | None = None
    runtime: int | None = Field(default=None, ge=0)
    original_language: str | None = Field(default=None, max_length=20)
    overview: str | None = None
    popularity: float | None = None
    vote_average: float | None = None
    vote_count: int | None = Field(default=None, ge=0)
    imdb_id: str | None = Field(default=None, max_length=50)
    tmdb_id: str | None = Field(default=None, max_length=50)
    license_cost: Decimal | None = Field(default=None, ge=0)


class MovieUpdate(DatabaseSchema):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    genres: str | None = Field(default=None, max_length=1000)
    release_date: date | None = None
    runtime: int | None = Field(default=None, ge=0)
    original_language: str | None = Field(default=None, max_length=20)
    overview: str | None = None
    popularity: float | None = None
    vote_average: float | None = None
    vote_count: int | None = Field(default=None, ge=0)
    imdb_id: str | None = Field(default=None, max_length=50)
    tmdb_id: str | None = Field(default=None, max_length=50)
    license_cost: Decimal | None = Field(default=None, ge=0)


class StreamEventCreate(DatabaseSchema):
    event_id: UUID | None = None
    user_id: str = Field(min_length=1, max_length=255)
    event_type: Literal["watch", "rating", "account_created"]
    movie_id: str | None = Field(default=None, max_length=255)
    event_time: datetime
    minute_index: int | None = Field(default=None, ge=0)
    rating: float | None = Field(default=None, ge=1, le=10)

    @model_validator(mode="before")
    @classmethod
    def normalize_source_event(cls, data):
        if not isinstance(data, dict):
            return data
        values = dict(data)
        if "timestamp" in values:
            timestamp = values.pop("timestamp")
            if "event_time" in values and values["event_time"] != timestamp:
                raise ValueError("timestamp and event_time conflict")
            values.setdefault("event_time", timestamp)
        for field_name in ("movie_id", "rating"):
            value = values.get(field_name)
            if value == "" or (isinstance(value, float) and isnan(value)):
                values[field_name] = None
        if "user_id" in values:
            user_id = values["user_id"]

            if user_id is None:
                raise ValueError("user_id cannot be None")

            values["user_id"] = str(user_id).strip()
        if isinstance(values.get("movie_id"), str):
            values["movie_id"] = values["movie_id"].strip() or None
        return values

    @model_validator(mode="after")
    def validate_event_shape(self):
        if self.event_time.tzinfo is None or self.event_time.utcoffset() is None:
            self.event_time = self.event_time.replace(tzinfo=UTC)
        else:
            self.event_time = self.event_time.astimezone(UTC)

        if self.event_type == "account_created":
            if self.movie_id is not None or self.rating is not None:
                raise ValueError(
                    "account_created events cannot include a movie or rating"
                )
        elif self.event_type == "watch":
            if self.movie_id is None or self.rating is not None:
                raise ValueError(
                    "watch events require a movie and cannot include a rating"
                )
        elif self.movie_id is None or self.rating is None:
            raise ValueError("rating events require a movie and rating")
        return self


class RecommendationItem(DatabaseSchema):
    movie_id: str = Field(min_length=1, max_length=255)
    model_score: FiniteFloat | None = Field(default=None, ge=-1, le=1)

    @field_validator("model_score", mode="before")
    @classmethod
    def reject_boolean_score(cls, value):
        if isinstance(value, bool):
            raise ValueError(
                "model_score must be a finite number in [-1, 1], not a boolean"
            )
        return value


class CourseRequestCreate(DatabaseSchema):
    topic: str = Field(min_length=1, max_length=255)
    partition_id: int = Field(ge=0)
    offset_id: int = Field(ge=0)
    received_at: datetime | None = None
    raw_line: str


class RecommendationServeCreate(DatabaseSchema):
    request_id: UUID | None = None
    experiment_assignment_id: UUID | None = None
    user_id: str = Field(min_length=1, max_length=255)
    served_at: datetime
    model_route: str = Field(min_length=1, max_length=100)
    model_version: str = Field(min_length=1, max_length=255)
    recommendations: list[RecommendationItem] = Field(min_length=1, max_length=20)
    request_received_at: datetime
    serve_duration_ms: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_timing(self):
        if self.served_at < self.request_received_at:
            raise ValueError("served_at cannot precede request_received_at")
        return self


RejectionReason = Literal[
    "invalid_encoding",
    "malformed_line",
    "unrecognized_event",
    "invalid_user_id",
    "invalid_event_time",
    "invalid_field",
    "unknown_movie",
]
# Monitoring keeps one count column per reason; add a column when adding one here.
REJECTION_REASONS: tuple[str, ...] = get_args(RejectionReason)
RAW_LINE_LIMIT = 1024
_DETAIL_TEXT_LIMIT = 500


def _storable_text(value: str) -> str:
    """Escape what PostgreSQL text cannot hold (NUL, lone surrogates) visibly."""
    return (
        value.encode("utf-8", "backslashreplace")
        .decode("utf-8")
        .replace("\x00", "\\x00")
    )


def _rejection_error_detail(error: object) -> dict[str, Any]:
    if not isinstance(error, Mapping):
        raise ValueError("Each error must be a mapping with loc and type")
    location, error_type, message = (
        error.get("loc"),
        error.get("type"),
        error.get("msg"),
    )
    if not isinstance(location, (list, tuple)) or not all(
        isinstance(part, (str, int)) and not isinstance(part, bool) for part in location
    ):
        raise ValueError("Each error needs a loc sequence of names or indexes")
    if not isinstance(error_type, str) or not error_type:
        raise ValueError("Each error needs a non-empty string type")
    if message is not None and not isinstance(message, str):
        raise ValueError("Error msg must be a string when present")
    detail = {
        "loc": [
            _storable_text(part)[:100] if isinstance(part, str) else part
            for part in location
        ],
        "type": _storable_text(error_type)[:100],
    }
    if message is not None:
        detail["msg"] = _storable_text(message)[:_DETAIL_TEXT_LIMIT]
    return detail


class RejectedRequestCreate(DatabaseSchema):
    """One Kafka course-log record the consumer rejected, keyed by its position."""

    topic: str = Field(min_length=1, max_length=255)
    partition_id: int = Field(ge=0)
    offset_id: int = Field(ge=0)
    received_at: datetime | None = None
    event_time: datetime | None = None
    event_kind: Literal["account", "watch", "rating", "recommendation", "unknown"] = (
        "unknown"
    )
    reason: RejectionReason
    user_id: str | None = None
    error_details: list[dict] = Field(default_factory=list)
    raw_line: str
    raw_line_truncated: bool = False

    @property
    def event_id(self) -> UUID:
        """Same uuid5 the consumer gives stream events from this Kafka position."""
        return uuid5(
            NAMESPACE_URL, f"kafka:{self.topic}:{self.partition_id}:{self.offset_id}"
        )

    @field_validator("partition_id", "offset_id", mode="before")
    @classmethod
    def require_integer_position(cls, value):
        if type(value) is not int:
            raise ValueError("Kafka positions must be integers")
        return value

    @field_validator("raw_line", mode="before")
    @classmethod
    def decode_raw_line(cls, value):
        # Undecodable Kafka payloads are kept with their bad bytes escaped.
        if isinstance(value, (bytes, bytearray)):
            value = bytes(value).decode("utf-8", "backslashreplace")
        if not isinstance(value, str):
            raise ValueError("raw_line must be text or bytes")
        return _storable_text(value)

    @field_validator("user_id", mode="before")
    @classmethod
    def keep_user_id_as_given(cls, value):
        # Stored even when invalid, so malformed identifiers can be diagnosed.
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("user_id must be text")
        return _storable_text(value)[:255]

    @field_validator("error_details", mode="before")
    @classmethod
    def normalize_error_details(cls, value):
        if value is None:
            return []
        if not isinstance(value, (list, tuple)):
            raise ValueError("error_details must be a list of structured errors")
        return [_rejection_error_detail(error) for error in value]

    @model_validator(mode="after")
    def normalize(self):
        for name in ("received_at", "event_time"):
            value = getattr(self, name)
            if value is not None:
                if value.utcoffset() is None:
                    raise ValueError(f"{name} must include a timezone")
                setattr(self, name, value.astimezone(UTC))
        if len(self.raw_line) > RAW_LINE_LIMIT:
            self.raw_line = self.raw_line[:RAW_LINE_LIMIT]
            self.raw_line_truncated = True
        return self


class LLMUsageCreate(DatabaseSchema):
    request_id: UUID | None = None
    user_id: str | None = Field(default=None, max_length=255)
    occurred_at: datetime | None = None
    provider: str = Field(min_length=1, max_length=100)
    model_name: str = Field(min_length=1, max_length=255)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: Decimal | None = Field(default=None, ge=0)
    status: str = Field(min_length=1, max_length=50)


class ColdStartProfileReplace(DatabaseSchema):
    user_id: str = Field(min_length=1, max_length=255)
    positive: str = ""
    negative: str = ""
    source_description_hash: str | None = Field(
        default=None, min_length=64, max_length=64
    )
    llm_request_id: UUID | None = None
    generated_at: datetime | None = None


class ColdStartJobCreate(DatabaseSchema):
    user_id: str = Field(min_length=1, max_length=255)
    status: Literal["pending", "processing"] = "pending"
    attempts: int = Field(default=0, ge=0)
    next_attempt_at: datetime | None = None
    lease_until: datetime | None = None
    last_error: str | None = None


class ExperimentCreate(DatabaseSchema):
    experiment_id: UUID | None = None
    name: str = Field(min_length=1, max_length=255)
    status: Literal["draft", "running", "paused", "completed", "aborted"] = "draft"
    created_at: datetime | None = None
    updated_at: datetime | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    control_policy: str = Field(min_length=1, max_length=255)
    control_version: str = Field(min_length=1, max_length=255)
    treatment_policy: str = Field(min_length=1, max_length=255)
    treatment_version: str = Field(min_length=1, max_length=255)
    traffic_allocation: Decimal = Field(
        default=Decimal("0"), ge=0, le=1, max_digits=7, decimal_places=6
    )
    treatment_allocation: Decimal = Field(
        default=Decimal("0.5"), ge=0, le=1, max_digits=7, decimal_places=6
    )
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "name",
        "control_policy",
        "control_version",
        "treatment_policy",
        "treatment_version",
    )
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def validate_times(self):
        _normalize_experiment_times(
            self, ("created_at", "updated_at", "starts_at", "ends_at")
        )
        if self.ends_at is not None and (
            self.starts_at is None or self.ends_at <= self.starts_at
        ):
            raise ValueError("ends_at requires starts_at and must be later")
        if (
            self.created_at is not None
            and self.updated_at is not None
            and self.updated_at < self.created_at
        ):
            raise ValueError("updated_at cannot precede created_at")
        return self


def _normalize_experiment_times(record, fields):
    for name in fields:
        value = getattr(record, name)
        if value is not None:
            if value.utcoffset() is None:
                raise ValueError(f"{name} must include a timezone")
            setattr(record, name, value.astimezone(UTC))


class ExperimentAssignmentCreate(DatabaseSchema):
    assignment_id: UUID | None = None
    experiment_id: UUID
    user_id: str = Field(min_length=1, max_length=255)
    variant: Literal["control", "treatment"]
    assigned_at: datetime | None = None

    @field_validator("user_id")
    @classmethod
    def nonblank_user(cls, value):
        if not value.strip():
            raise ValueError("user_id must not be blank")
        return value

    @model_validator(mode="after")
    def validate_time(self):
        _normalize_experiment_times(self, ("assigned_at",))
        return self


class ExperimentReportCreate(DatabaseSchema):
    report_id: UUID | None = None
    experiment_id: UUID
    generated_at: datetime | None = None
    window_start: datetime
    window_end: datetime
    status: Literal["provisional", "final", "insufficient_data", "failed"] = (
        "provisional"
    )
    results: dict[str, Any] = Field(default_factory=dict)
    analysis_version: str = Field(min_length=1, max_length=255)

    @field_validator("analysis_version")
    @classmethod
    def nonblank_version(cls, value):
        if not value.strip():
            raise ValueError("analysis_version must not be blank")
        return value

    @model_validator(mode="after")
    def validate_times(self):
        _normalize_experiment_times(
            self, ("generated_at", "window_start", "window_end")
        )
        if self.window_end <= self.window_start:
            raise ValueError("window_end must be later than window_start")
        return self


class ExperimentLifecycleUpdate(DatabaseSchema):
    experiment_id: UUID
    status: Literal["draft", "running", "paused", "completed", "aborted"]
    occurred_at: datetime

    @model_validator(mode="after")
    def validate_time(self):
        _normalize_experiment_times(self, ("occurred_at",))
        return self


class ExperimentDecisionRequest(DatabaseSchema):
    experiment_id: UUID
    user_id: str = Field(min_length=1, max_length=255)
    eligible: StrictBool
    now: datetime

    @field_validator("user_id")
    @classmethod
    def nonblank_user(cls, value):
        if not value.strip():
            raise ValueError("user_id must not be blank")
        return value

    @model_validator(mode="after")
    def validate_time(self):
        _normalize_experiment_times(self, ("now",))
        return self
