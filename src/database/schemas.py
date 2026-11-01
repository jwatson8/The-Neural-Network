"""Validated input objects accepted by the database interaction layer."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import ConfigDict
from sqlmodel import Field, SQLModel


class DatabaseSchema(SQLModel):
    model_config = ConfigDict(extra="forbid")


class UserCreate(DatabaseSchema):
    user_id: str = Field(min_length=1, max_length=255)
    is_active: bool = True


class MovieCreate(DatabaseSchema):
    movie_id: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=500)
    genres: list[str] = Field(default_factory=list)
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class StreamEventCreate(DatabaseSchema):
    user_id: str = Field(min_length=1, max_length=255)
    movie_id: str | None = Field(default=None, max_length=255)
    event_type: str = Field(min_length=1, max_length=100)
    occurred_at: datetime | None = None
    rating: float | None = Field(default=None, ge=0, le=5)
    event_data: dict[str, Any] = Field(default_factory=dict)


class RecommendationServeCreate(DatabaseSchema):
    request_id: UUID
    user_id: str = Field(min_length=1, max_length=255)
    movie_id: str = Field(min_length=1, max_length=255)
    rank: int = Field(gt=0)
    score: float | None = None
    model_version: str = Field(min_length=1, max_length=255)
    served_at: datetime | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class UserMovieHistoryCreate(DatabaseSchema):
    user_id: str = Field(min_length=1, max_length=255)
    movie_id: str = Field(min_length=1, max_length=255)
    first_watched_at: datetime | None = None
    last_watched_at: datetime | None = None
    watch_count: int = Field(default=0, ge=0)
    completion_ratio: float | None = Field(default=None, ge=0, le=1)
    rating: float | None = Field(default=None, ge=0, le=5)


class LLMUsageCreate(DatabaseSchema):
    user_id: str | None = Field(default=None, max_length=255)
    request_id: UUID | None = None
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=255)
    operation: str = Field(min_length=1, max_length=100)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    usage_data: dict[str, Any] = Field(default_factory=dict)


class UserUpdate(DatabaseSchema):
    is_active: bool | None = None


class MovieUpdate(DatabaseSchema):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    genres: list[str] | None = None
    metadata_json: dict[str, Any] | None = None


class StreamEventUpdate(DatabaseSchema):
    event_type: str | None = Field(default=None, min_length=1, max_length=100)
    rating: float | None = Field(default=None, ge=0, le=5)
    event_data: dict[str, Any] | None = None


class RecommendationServeUpdate(DatabaseSchema):
    score: float | None = None
    context: dict[str, Any] | None = None


class UserMovieHistoryUpdate(DatabaseSchema):
    first_watched_at: datetime | None = None
    last_watched_at: datetime | None = None
    watch_count: int | None = Field(default=None, ge=0)
    completion_ratio: float | None = Field(default=None, ge=0, le=1)
    rating: float | None = Field(default=None, ge=0, le=5)


class LLMUsageUpdate(DatabaseSchema):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    usage_data: dict[str, Any] | None = None
