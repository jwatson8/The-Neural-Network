"""SQLModel table definitions for recommendation service data."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Column, DateTime, Index, JSON, UniqueConstraint
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    return datetime.now(UTC)


class TimestampedModel(SQLModel):
    created_at: datetime = Field(
        default_factory=utc_now,
        sa_type=DateTime(timezone=True),
        nullable=False,
    )
    updated_at: datetime = Field(
        default_factory=utc_now,
        sa_type=DateTime(timezone=True),
        nullable=False,
    )


class User(TimestampedModel, table=True):
    __tablename__ = "users"

    user_id: str = Field(primary_key=True, max_length=255)
    is_active: bool = Field(default=True, index=True)


class Movie(TimestampedModel, table=True):
    __tablename__ = "movies"

    movie_id: str = Field(primary_key=True, max_length=255)
    title: str = Field(max_length=500, index=True)
    genres: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))


class StreamEvent(SQLModel, table=True):
    __tablename__ = "stream_events"
    __table_args__ = (
        CheckConstraint("rating IS NULL OR (rating >= 0 AND rating <= 5)", name="ck_stream_event_rating"),
        Index("ix_stream_events_user_occurred", "user_id", "occurred_at"),
    )

    event_id: UUID = Field(default_factory=uuid4, primary_key=True)
    occurred_at: datetime = Field(
        default_factory=utc_now,
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True),
    )
    user_id: str = Field(foreign_key="users.user_id", max_length=255, index=True)
    movie_id: str | None = Field(default=None, foreign_key="movies.movie_id", max_length=255, index=True)
    event_type: str = Field(max_length=100, index=True)
    rating: float | None = None
    event_data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))


class RecommendationServe(SQLModel, table=True):
    __tablename__ = "recommendation_serves"
    __table_args__ = (
        CheckConstraint("rank > 0", name="ck_recommendation_serve_rank"),
        UniqueConstraint("request_id", "rank", name="uq_recommendation_request_rank"),
        UniqueConstraint("request_id", "movie_id", name="uq_recommendation_request_movie"),
        Index("ix_recommendation_serves_user_served", "user_id", "served_at"),
    )

    serve_id: UUID = Field(default_factory=uuid4, primary_key=True)
    request_id: UUID = Field(index=True)
    served_at: datetime = Field(
        default_factory=utc_now,
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True),
    )
    user_id: str = Field(foreign_key="users.user_id", max_length=255, index=True)
    movie_id: str = Field(foreign_key="movies.movie_id", max_length=255, index=True)
    rank: int
    score: float | None = None
    model_version: str = Field(max_length=255)
    context: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))


class UserMovieHistory(SQLModel, table=True):
    __tablename__ = "user_movie_history"
    __table_args__ = (
        CheckConstraint("watch_count >= 0", name="ck_history_watch_count"),
        CheckConstraint(
            "completion_ratio IS NULL OR (completion_ratio >= 0 AND completion_ratio <= 1)",
            name="ck_history_completion_ratio",
        ),
        UniqueConstraint("user_id", "movie_id", name="uq_history_user_movie"),
    )

    history_id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: str = Field(foreign_key="users.user_id", max_length=255, index=True)
    movie_id: str = Field(foreign_key="movies.movie_id", max_length=255, index=True)
    first_watched_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    last_watched_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True), index=True)
    watch_count: int = 0
    completion_ratio: float | None = None
    rating: float | None = None


class LLMUsage(SQLModel, table=True):
    __tablename__ = "llm_usage"
    __table_args__ = (
        CheckConstraint("input_tokens >= 0", name="ck_llm_input_tokens"),
        CheckConstraint("output_tokens >= 0", name="ck_llm_output_tokens"),
        CheckConstraint("cost_usd IS NULL OR cost_usd >= 0", name="ck_llm_cost"),
        Index("ix_llm_usage_user_created", "user_id", "created_at"),
    )

    usage_id: UUID = Field(default_factory=uuid4, primary_key=True)
    created_at: datetime = Field(
        default_factory=utc_now,
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True),
    )
    user_id: str | None = Field(default=None, foreign_key="users.user_id", max_length=255, index=True)
    request_id: UUID | None = Field(default=None, index=True)
    provider: str = Field(max_length=100)
    model: str = Field(max_length=255, index=True)
    operation: str = Field(max_length=100, index=True)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    usage_data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
