"""Public, table-specific database operations for application code."""

from datetime import timedelta
from uuid import UUID

from sqlalchemy import and_, or_
from sqlmodel import Session, SQLModel, select

from src.database.models import (
    ColdStartJob,
    ColdStartProfile,
    LLMUsage,
    Movie,
    RecommendationServe,
    StreamEvent,
    User,
    UserMovieHistory,
    utc_now,
)
from src.database.repository import create_record, get_record, list_records, update_record
from src.database.schemas import (
    ColdStartJobCreate,
    ColdStartProfileUpsert,
    LLMUsageCreate,
    MovieCreate,
    MovieUpdate,
    RecommendationServeCreate,
    StreamEventCreate,
    UserCreate,
    UserMovieHistoryUpsert,
    UserUpdate,
)


def _values(schema: SQLModel) -> dict[str, object]:
    return schema.model_dump(exclude_none=True)


def _dialect_insert(session: Session, model_type: type[SQLModel]):
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError(f"Unsupported insert dialect: {dialect_name}")
    return insert(model_type)


def _upsert(
    session: Session,
    model_type: type[SQLModel],
    values: dict[str, object],
    key_fields: tuple[str, ...],
    update_fields: tuple[str, ...],
):
    statement = _dialect_insert(session, model_type).values(**values)
    statement = statement.on_conflict_do_update(
        index_elements=list(key_fields),
        set_={field: getattr(statement.excluded, field) for field in update_fields},
    )
    session.execute(statement)
    session.flush()
    identity = tuple(values[field] for field in key_fields)
    record = session.get(model_type, identity[0] if len(identity) == 1 else identity)
    session.refresh(record)
    return record


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


def list_stream_events(
    session: Session, *, offset: int = 0, limit: int = 100
) -> list[StreamEvent]:
    return list_records(session, StreamEvent, offset=offset, limit=limit)


def upsert_user_movie_history(
    session: Session, data: UserMovieHistoryUpsert
) -> UserMovieHistory:
    values = _values(data)
    values.setdefault("stored_at", utc_now())
    return _upsert(
        session,
        UserMovieHistory,
        values,
        ("user_id", "movie_id"),
        ("latest_rating", "stored_at"),
    )


def get_user_movie_history(
    session: Session, user_id: str, movie_id: str
) -> UserMovieHistory | None:
    return session.get(UserMovieHistory, (user_id, movie_id))


def list_user_movie_history(
    session: Session, *, offset: int = 0, limit: int = 100
) -> list[UserMovieHistory]:
    return list_records(session, UserMovieHistory, offset=offset, limit=limit)


def create_recommendation_serve(
    session: Session, data: RecommendationServeCreate
) -> RecommendationServe:
    return create_record(session, RecommendationServe, _values(data))


def get_recommendation_serve(
    session: Session, request_id: UUID
) -> RecommendationServe | None:
    return get_record(session, RecommendationServe, request_id)


def list_recommendation_serves(
    session: Session, *, offset: int = 0, limit: int = 100
) -> list[RecommendationServe]:
    return list_records(session, RecommendationServe, offset=offset, limit=limit)


def create_llm_usage(session: Session, data: LLMUsageCreate) -> LLMUsage:
    return create_record(session, LLMUsage, _values(data))


def get_llm_usage(session: Session, request_id: UUID) -> LLMUsage | None:
    return get_record(session, LLMUsage, request_id)


def list_llm_usage(
    session: Session, *, offset: int = 0, limit: int = 100
) -> list[LLMUsage]:
    return list_records(session, LLMUsage, offset=offset, limit=limit)


def upsert_cold_start_profile(
    session: Session, data: ColdStartProfileUpsert
) -> ColdStartProfile:
    values = _values(data)
    values.setdefault("generated_at", utc_now())
    values["updated_at"] = utc_now()
    return _upsert(
        session,
        ColdStartProfile,
        values,
        ("user_id",),
        (
            "positive",
            "negative",
            "source_description_hash",
            "llm_request_id",
            "generated_at",
            "updated_at",
        ),
    )


def get_cold_start_profile(
    session: Session, user_id: str
) -> ColdStartProfile | None:
    return get_record(session, ColdStartProfile, user_id)


def list_cold_start_profiles(
    session: Session, *, offset: int = 0, limit: int = 100
) -> list[ColdStartProfile]:
    return list_records(session, ColdStartProfile, offset=offset, limit=limit)


def enqueue_cold_start_job(
    session: Session, data: ColdStartJobCreate
) -> ColdStartJob:
    values = _values(data)
    now = utc_now()
    values.setdefault("next_attempt_at", now)
    values.setdefault("created_at", now)
    values.setdefault("updated_at", now)
    statement = _dialect_insert(session, ColdStartJob).values(**values)
    session.execute(statement.on_conflict_do_nothing(index_elements=["user_id"]))
    session.flush()
    return get_record(session, ColdStartJob, data.user_id)


def claim_cold_start_job(
    session: Session, *, lease_seconds: int = 60
) -> ColdStartJob | None:
    now = utc_now()
    statement = (
        select(ColdStartJob)
        .where(
            ColdStartJob.next_attempt_at <= now,
            or_(
                ColdStartJob.status == "pending",
                and_(
                    ColdStartJob.status == "processing",
                    ColdStartJob.lease_until <= now,
                ),
            ),
        )
        .order_by(ColdStartJob.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = session.exec(statement).first()
    if job is None:
        return None
    job.status = "processing"
    job.attempts += 1
    job.lease_until = now + timedelta(seconds=lease_seconds)
    job.updated_at = now
    job.last_error = None
    session.add(job)
    session.flush()
    session.refresh(job)
    return job


def retry_cold_start_job(
    session: Session,
    job: ColdStartJob,
    error: str,
    *,
    max_delay_seconds: int = 900,
) -> ColdStartJob:
    now = utc_now()
    delay = min(2 ** min(job.attempts, 10), max_delay_seconds)
    job.status = "pending"
    job.next_attempt_at = now + timedelta(seconds=delay)
    job.lease_until = None
    job.updated_at = now
    job.last_error = error
    session.add(job)
    session.flush()
    session.refresh(job)
    return job


def complete_cold_start_job(session: Session, user_id: str) -> bool:
    job = get_record(session, ColdStartJob, user_id)
    if job is None:
        return False
    session.delete(job)
    session.flush()
    return True
