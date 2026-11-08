"""Validated input objects accepted by the database interaction layer."""

from datetime import UTC, date, datetime
from decimal import Decimal
from math import isnan
from typing import Any, Literal
from uuid import UUID

from pydantic import ConfigDict, FiniteFloat, field_validator, model_validator
from sqlmodel import Field, SQLModel


class DatabaseSchema(SQLModel):
    model_config = ConfigDict(extra="forbid")


class DataQualityLogCreate(DatabaseSchema):
    occurred_at: datetime | None = None
    source: Literal["kafka", "user_api", "movie_api", "recommendation_api", "drift"]
    severity: Literal["INFO", "WARNING", "ERROR"]
    issue_type: Literal["validation_failure", "drift_result"]
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
            raise ValueError("model_score must be a finite number in [-1, 1], not a boolean")
        return value


class RecommendationServeCreate(DatabaseSchema):
    request_id: UUID | None = None
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
