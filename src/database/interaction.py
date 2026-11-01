"""Public, table-specific database operations for application code."""

from uuid import UUID

from sqlmodel import Session, SQLModel

from src.database.models import LLMUsage, Movie, RecommendationServe, StreamEvent, User, UserMovieHistory
from src.database.repository import create_record, get_record, list_records, update_record
from src.database.schemas import (
    LLMUsageCreate,
    LLMUsageUpdate,
    MovieCreate,
    MovieUpdate,
    RecommendationServeCreate,
    RecommendationServeUpdate,
    StreamEventCreate,
    StreamEventUpdate,
    UserCreate,
    UserMovieHistoryUpdate,
    UserUpdate,
    UserMovieHistoryCreate,
)


def _values(schema: SQLModel) -> dict[str, object]:
    return schema.model_dump(exclude_none=True)


def create_user(session: Session, data: UserCreate) -> User:
    return create_record(session, User, _values(data))


def get_user(session: Session, user_id: str) -> User | None:
    return get_record(session, User, user_id)


def update_user(session: Session, user: User, data: UserUpdate) -> User:
    return update_record(session, user, data.model_dump(exclude_unset=True))


def list_users(session: Session, *, offset: int = 0, limit: int = 100) -> list[User]:
    return list_records(session, User, offset=offset, limit=limit)


def create_movie(session: Session, data: MovieCreate) -> Movie:
    return create_record(session, Movie, _values(data))


def get_movie(session: Session, movie_id: str) -> Movie | None:
    return get_record(session, Movie, movie_id)


def update_movie(session: Session, movie: Movie, data: MovieUpdate) -> Movie:
    return update_record(session, movie, data.model_dump(exclude_unset=True))


def list_movies(session: Session, *, offset: int = 0, limit: int = 100) -> list[Movie]:
    return list_records(session, Movie, offset=offset, limit=limit)


def create_stream_event(session: Session, data: StreamEventCreate) -> StreamEvent:
    return create_record(session, StreamEvent, _values(data))


def get_stream_event(session: Session, event_id: UUID) -> StreamEvent | None:
    return get_record(session, StreamEvent, event_id)


def update_stream_event(
    session: Session, event: StreamEvent, data: StreamEventUpdate
) -> StreamEvent:
    return update_record(session, event, data.model_dump(exclude_unset=True))


def list_stream_events(
    session: Session, *, offset: int = 0, limit: int = 100
) -> list[StreamEvent]:
    return list_records(session, StreamEvent, offset=offset, limit=limit)


def create_recommendation_serve(
    session: Session, data: RecommendationServeCreate
) -> RecommendationServe:
    return create_record(session, RecommendationServe, _values(data))


def get_recommendation_serve(session: Session, serve_id: UUID) -> RecommendationServe | None:
    return get_record(session, RecommendationServe, serve_id)


def update_recommendation_serve(
    session: Session,
    serve: RecommendationServe,
    data: RecommendationServeUpdate,
) -> RecommendationServe:
    return update_record(session, serve, data.model_dump(exclude_unset=True))


def list_recommendation_serves(
    session: Session, *, offset: int = 0, limit: int = 100
) -> list[RecommendationServe]:
    return list_records(session, RecommendationServe, offset=offset, limit=limit)


def create_user_movie_history(
    session: Session, data: UserMovieHistoryCreate
) -> UserMovieHistory:
    return create_record(session, UserMovieHistory, _values(data))


def get_user_movie_history(session: Session, history_id: UUID) -> UserMovieHistory | None:
    return get_record(session, UserMovieHistory, history_id)


def update_user_movie_history(
    session: Session,
    history: UserMovieHistory,
    data: UserMovieHistoryUpdate,
) -> UserMovieHistory:
    return update_record(session, history, data.model_dump(exclude_unset=True))


def list_user_movie_history(
    session: Session, *, offset: int = 0, limit: int = 100
) -> list[UserMovieHistory]:
    return list_records(session, UserMovieHistory, offset=offset, limit=limit)


def create_llm_usage(session: Session, data: LLMUsageCreate) -> LLMUsage:
    return create_record(session, LLMUsage, _values(data))


def get_llm_usage(session: Session, usage_id: UUID) -> LLMUsage | None:
    return get_record(session, LLMUsage, usage_id)


def update_llm_usage(session: Session, usage: LLMUsage, data: LLMUsageUpdate) -> LLMUsage:
    return update_record(session, usage, data.model_dump(exclude_unset=True))


def list_llm_usage(session: Session, *, offset: int = 0, limit: int = 100) -> list[LLMUsage]:
    return list_records(session, LLMUsage, offset=offset, limit=limit)
