"""SQLModel engine, pooling, and session lifecycle configuration."""

import os
from collections.abc import Generator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import URL, Engine, text
from sqlmodel import Session, create_engine


def get_database_url() -> str | URL:
    if database_url := os.getenv("DATABASE_URL"):
        return database_url
    password = os.getenv("DATABASE_PASSWORD")
    if not password:
        raise RuntimeError("Set DATABASE_PASSWORD or DATABASE_URL")
    return URL.create(
        "postgresql+psycopg",
        username=os.getenv("DATABASE_USER", "recommender"),
        password=password,
        host=os.getenv("DATABASE_HOST", "database"),
        port=int(os.getenv("DATABASE_PORT", "5432")),
        database=os.getenv("DATABASE_NAME", "recommender"),
    )


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    database_url = get_database_url()
    engine_options: dict[str, object] = {
        "echo": os.getenv("DATABASE_ECHO", "false").lower() == "true",
        "pool_pre_ping": True,
    }
    if not str(database_url).startswith("sqlite"):
        engine_options.update(
            pool_size=int(os.getenv("DATABASE_POOL_SIZE", "5")),
            max_overflow=int(os.getenv("DATABASE_MAX_OVERFLOW", "10")),
            pool_timeout=int(os.getenv("DATABASE_POOL_TIMEOUT_SECONDS", "30")),
            pool_recycle=int(os.getenv("DATABASE_POOL_RECYCLE_SECONDS", "1800")),
        )
    return create_engine(database_url, **engine_options)


def get_session() -> Generator[Session, None, None]:
    """Yield a session for dependency-injection frameworks."""
    with Session(get_engine(), expire_on_commit=False) as session:
        yield session


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Provide a transaction that commits on success and rolls back on error."""
    with Session(get_engine(), expire_on_commit=False) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def database_is_ready() -> bool:
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def reset_engine() -> None:
    """Clear cached engine state, primarily for tests and configuration changes."""
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_engine.cache_clear()
