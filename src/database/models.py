"""SQLModel table definitions for recommendation service data."""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    return datetime.now(UTC)


JSON_DOCUMENT = JSONB().with_variant(JSON(), "sqlite")
NULLABLE_JSON_DOCUMENT = JSONB(none_as_null=True).with_variant(
    JSON(none_as_null=True), "sqlite"
)


class DataQualityLog(SQLModel, table=True):
    __tablename__ = "data_quality_logs"

    log_id: UUID = Field(default_factory=uuid4, primary_key=True)
    occurred_at: datetime = Field(
        default_factory=utc_now,
        sa_type=DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    source: str = Field(max_length=100, index=True)
    severity: str = Field(max_length=20, index=True)
    issue_type: str = Field(max_length=100, index=True)
    error_details: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON_DOCUMENT, nullable=False)
    )
    log_metadata: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON_DOCUMENT, nullable=False)
    )
    # Deliberately not foreign keys: rejected records may refer to absent rows.
    user_id: str | None = Field(default=None, max_length=255, index=True)
    movie_id: str | None = Field(default=None, max_length=255, index=True)
    event_id: UUID | None = Field(default=None, index=True)
    request_id: UUID | None = Field(default=None, index=True)


class ModelLog(SQLModel, table=True):
    __tablename__ = "model_logs"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('train', 'test')",
            name="ck_model_log_event_type",
        ),
        CheckConstraint(
            "training_data_source IS NULL OR "
            "training_data_source IN ('stream_events', 'user_movie_history')",
            name="ck_model_log_training_source",
        ),
        CheckConstraint(
            "training_data_end_at IS NULL OR training_data_start_at IS NULL OR "
            "training_data_end_at > training_data_start_at",
            name="ck_model_log_training_window",
        ),
        CheckConstraint(
            "training_row_count IS NULL OR training_row_count >= 0",
            name="ck_model_log_training_row_count",
        ),
        CheckConstraint(
            "model_size_bytes IS NULL OR model_size_bytes >= 0",
            name="ck_model_log_model_size",
        ),
        CheckConstraint(
            "(event_type = 'train' AND training_data_source IS NOT NULL AND "
            "training_data_start_at IS NOT NULL AND training_data_end_at IS NOT NULL AND "
            "training_row_count IS NOT NULL AND test_type IS NULL AND test_result IS NULL) OR "
            "(event_type = 'test' AND training_data_source IS NULL AND "
            "training_data_start_at IS NULL AND training_data_end_at IS NULL AND "
            "training_row_count IS NULL AND model_size_bytes IS NULL AND "
            "test_type IS NOT NULL AND test_result IS NOT NULL)",
            name="ck_model_log_event_shape",
        ),
        Index("ix_model_logs_model_occurred", "model_name", "occurred_at"),
    )

    log_id: UUID = Field(default_factory=uuid4, primary_key=True)
    event_type: str = Field(max_length=20, index=True)
    occurred_at: datetime = Field(
        default_factory=utc_now,
        sa_type=DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    model_name: str = Field(max_length=255, index=True)
    training_data_source: str | None = Field(default=None, max_length=50, index=True)
    training_data_start_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True)
    )
    training_data_end_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True)
    )
    training_row_count: int | None = Field(default=None, ge=0)
    model_size_bytes: int | None = Field(default=None, ge=0)
    test_type: str | None = Field(default=None, max_length=100, index=True)
    test_result: dict[str, Any] | None = Field(
        default=None, sa_column=Column(NULLABLE_JSON_DOCUMENT, nullable=True)
    )


class User(SQLModel, table=True):
    __tablename__ = "users"

    user_id: str = Field(primary_key=True, max_length=255)
    first_seen: datetime = Field(
        default_factory=utc_now,
        sa_type=DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    age: int | None = Field(default=None, ge=0)
    occupation: str | None = Field(default=None, max_length=255)
    gender: str | None = Field(default=None, max_length=50)
    self_description_likes: str | None = Field(default=None, sa_type=Text)
    self_description_dislikes: str | None = Field(default=None, sa_type=Text)


class Movie(SQLModel, table=True):
    __tablename__ = "movies"

    movie_id: str = Field(primary_key=True, max_length=255)
    title: str = Field(max_length=500, index=True)
    genres: str | None = Field(default=None, max_length=1000)
    release_date: date | None = None
    runtime: int | None = Field(default=None, ge=0)
    original_language: str | None = Field(default=None, max_length=20)
    overview: str | None = Field(default=None, sa_type=Text)
    popularity: float | None = None
    vote_average: float | None = None
    vote_count: int | None = Field(default=None, ge=0)
    imdb_id: str | None = Field(default=None, max_length=50, index=True)
    tmdb_id: str | None = Field(default=None, max_length=50, index=True)
    license_cost: Decimal | None = Field(
        default=None, max_digits=16, decimal_places=10, ge=0
    )


class StreamEvent(SQLModel, table=True):
    __tablename__ = "stream_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('watch', 'rating', 'account_created')",
            name="ck_stream_event_type",
        ),
        CheckConstraint(
            "(event_type = 'account_created' AND movie_id IS NULL AND rating IS NULL) OR "
            "(event_type = 'watch' AND movie_id IS NOT NULL AND rating IS NULL) OR "
            "(event_type = 'rating' AND movie_id IS NOT NULL AND rating IS NOT NULL)",
            name="ck_stream_event_shape",
        ),
        CheckConstraint(
            "rating IS NULL OR (rating >= 1 AND rating <= 10)",
            name="ck_stream_event_rating",
        ),
        CheckConstraint(
            "minute_index IS NULL OR minute_index >= 0", name="ck_stream_event_minute"
        ),
        Index("ix_stream_events_user_event_time", "user_id", "event_time"),
    )

    event_id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: str = Field(foreign_key="users.user_id", max_length=255, index=True)
    event_type: str = Field(max_length=100, index=True)
    movie_id: str | None = Field(
        default=None, foreign_key="movies.movie_id", max_length=255, index=True
    )
    event_time: datetime = Field(
        sa_type=DateTime(timezone=True), nullable=False, index=True
    )
    stored_at: datetime = Field(
        default_factory=utc_now,
        sa_type=DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    minute_index: int | None = Field(default=None, ge=0)
    rating: float | None = Field(default=None, ge=1, le=10)


class CourseRequest(SQLModel, table=True):
    __tablename__ = "course_requests"
    __table_args__ = (
        CheckConstraint("partition_id >= 0", name="ck_course_request_partition"),
        CheckConstraint("offset_id >= 0", name="ck_course_request_offset"),
    )

    topic: str = Field(primary_key=True, max_length=255)
    partition_id: int = Field(primary_key=True, ge=0)
    offset_id: int = Field(primary_key=True, ge=0)
    received_at: datetime = Field(default_factory=utc_now, sa_type=DateTime(timezone=True), nullable=False, index=True)
    raw_line: str = Field(sa_type=Text, nullable=False)


class UserMovieHistory(SQLModel, table=True):
    __tablename__ = "user_movie_history"
    __table_args__ = (
        CheckConstraint(
            "latest_rating IS NULL OR (latest_rating >= 1 AND latest_rating <= 10)",
            name="ck_history_latest_rating",
        ),
        CheckConstraint(
            "last_recorded_watch_at IS NOT NULL OR last_recorded_rating_at IS NOT NULL",
            name="ck_history_has_activity",
        ),
        CheckConstraint(
            "(last_recorded_rating_at IS NULL AND latest_rating IS NULL AND "
            "rating_changed = FALSE) OR "
            "(last_recorded_rating_at IS NOT NULL AND latest_rating IS NOT NULL)",
            name="ck_history_rating_state",
        ),
        Index("ix_user_movie_history_last_watch", "last_recorded_watch_at"),
        Index("ix_user_movie_history_last_rating", "last_recorded_rating_at"),
    )

    user_id: str = Field(primary_key=True, foreign_key="users.user_id", max_length=255)
    movie_id: str = Field(
        primary_key=True, foreign_key="movies.movie_id", max_length=255
    )
    latest_rating: float | None = Field(default=None, ge=1, le=10)
    last_recorded_watch_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True)
    )
    last_recorded_rating_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True)
    )
    rating_changed: bool = Field(default=False, nullable=False)


class LicensingCost(SQLModel, table=True):
    __tablename__ = "licensing_costs"
    __table_args__ = (
        UniqueConstraint("source_event_id", name="uq_licensing_costs_source_event"),
        CheckConstraint(
            "license_cost IS NULL OR license_cost >= 0",
            name="ck_licensing_cost_non_negative",
        ),
        Index(
            "ix_licensing_costs_user_movie_charged",
            "user_id",
            "movie_id",
            "charged_at",
        ),
    )

    licensing_cost_id: UUID = Field(default_factory=uuid4, primary_key=True)
    source_event_id: UUID = Field(foreign_key="stream_events.event_id", index=True)
    charged_at: datetime = Field(
        sa_type=DateTime(timezone=True), nullable=False, index=True
    )
    user_id: str = Field(foreign_key="users.user_id", max_length=255, index=True)
    movie_id: str = Field(foreign_key="movies.movie_id", max_length=255, index=True)
    license_cost: Decimal | None = Field(
        default=None, max_digits=16, decimal_places=10, ge=0
    )


class RecommendationServe(SQLModel, table=True):
    __tablename__ = "recommendation_serves"
    __table_args__ = (
        CheckConstraint(
            "serve_duration_ms >= 0", name="ck_recommendation_serve_duration"
        ),
        Index("ix_recommendation_serves_user_served", "user_id", "served_at"),
    )

    request_id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: str = Field(foreign_key="users.user_id", max_length=255, index=True)
    served_at: datetime = Field(
        sa_type=DateTime(timezone=True), nullable=False, index=True
    )
    model_route: str = Field(max_length=100, index=True)
    model_version: str = Field(max_length=255, index=True)
    recommendations: list[dict[str, Any]] = Field(
        sa_column=Column(JSON_DOCUMENT, nullable=False)
    )
    request_received_at: datetime = Field(
        sa_type=DateTime(timezone=True), nullable=False
    )
    serve_duration_ms: float = Field(ge=0)


class LLMUsage(SQLModel, table=True):
    __tablename__ = "llm_usage"
    __table_args__ = (
        CheckConstraint("input_tokens >= 0", name="ck_llm_input_tokens"),
        CheckConstraint("output_tokens >= 0", name="ck_llm_output_tokens"),
        CheckConstraint(
            "estimated_cost_usd IS NULL OR estimated_cost_usd >= 0",
            name="ck_llm_estimated_cost",
        ),
        Index("ix_llm_usage_user_occurred", "user_id", "occurred_at"),
    )

    request_id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: str | None = Field(
        default=None, foreign_key="users.user_id", max_length=255, index=True
    )
    occurred_at: datetime = Field(
        default_factory=utc_now,
        sa_type=DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    provider: str = Field(max_length=100, index=True)
    model_name: str = Field(max_length=255, index=True)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: Decimal | None = Field(
        default=None, max_digits=14, decimal_places=8, ge=0
    )
    status: str = Field(max_length=50, index=True)


class MonitoringRun(SQLModel, table=True):
    __tablename__ = "monitoring_runs"
    __table_args__ = (Index("ix_monitoring_runs_window", "window_start", "window_end"),)

    run_id: UUID = Field(default_factory=uuid4, primary_key=True)
    generated_at: datetime = Field(
        sa_type=DateTime(timezone=True), nullable=False, index=True
    )
    window_start: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)
    window_end: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)
    licensing_charge_count: int = Field(ge=0)
    licensing_priced_charge_count: int = Field(ge=0)
    total_license_cost: Decimal = Field(max_digits=16, decimal_places=10, ge=0)
    llm_request_count: int = Field(ge=0)
    llm_priced_request_count: int = Field(ge=0)
    llm_input_tokens: int = Field(ge=0)
    llm_output_tokens: int = Field(ge=0)
    llm_estimated_cost_usd: Decimal = Field(max_digits=14, decimal_places=8, ge=0)
    evaluated_rating_count: int = Field(ge=0)
    attributed_rating_count: int = Field(ge=0)
    positive_rating_count: int = Field(ge=0)
    recommendation_accuracy: float | None = Field(default=None, ge=0, le=1)


class ColdStartProfile(SQLModel, table=True):
    __tablename__ = "cold_start_profiles"

    user_id: str = Field(primary_key=True, foreign_key="users.user_id", max_length=255)
    positive: str = Field(sa_type=Text)
    negative: str = Field(sa_type=Text)
    source_description_hash: str | None = Field(default=None, max_length=64, index=True)
    llm_request_id: UUID | None = Field(
        default=None, foreign_key="llm_usage.request_id", index=True
    )
    generated_at: datetime = Field(
        default_factory=utc_now, sa_type=DateTime(timezone=True), nullable=False
    )
    updated_at: datetime = Field(
        default_factory=utc_now, sa_type=DateTime(timezone=True), nullable=False
    )


class ColdStartJob(SQLModel, table=True):
    __tablename__ = "cold_start_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing')", name="ck_cold_start_job_status"
        ),
        CheckConstraint("attempts >= 0", name="ck_cold_start_job_attempts"),
        Index("ix_cold_start_jobs_claim", "status", "next_attempt_at", "lease_until"),
    )

    user_id: str = Field(primary_key=True, foreign_key="users.user_id", max_length=255)
    status: str = Field(default="pending", max_length=20)
    attempts: int = Field(default=0, ge=0)
    next_attempt_at: datetime = Field(
        default_factory=utc_now, sa_type=DateTime(timezone=True), nullable=False
    )
    lease_until: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    created_at: datetime = Field(
        default_factory=utc_now, sa_type=DateTime(timezone=True), nullable=False
    )
    updated_at: datetime = Field(
        default_factory=utc_now, sa_type=DateTime(timezone=True), nullable=False
    )
    last_error: str | None = Field(default=None, sa_type=Text)
