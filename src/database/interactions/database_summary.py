"""Aggregate row counts for operational database visibility."""

from sqlmodel import Session, select
from sqlalchemy import func

from src.database.interactions._shared import run
from src.database.models import (
    ColdStartJob,
    ColdStartProfile,
    CourseRequest,
    DataQualityLog,
    LicensingCost,
    LLMUsage,
    ModelLog,
    MonitoringRun,
    MonitoringModelAccuracy,
    Movie,
    RecommendationServe,
    StreamEvent,
    User,
    UserMovieHistory,
)

__all__ = ["counts"]

TABLES = {
    "users": User,
    "movies": Movie,
    "stream_events": StreamEvent,
    "course_requests": CourseRequest,
    "user_movie_history": UserMovieHistory,
    "recommendation_serves": RecommendationServe,
    "llm_usage": LLMUsage,
    "cold_start_profiles": ColdStartProfile,
    "cold_start_jobs": ColdStartJob,
    "data_quality_logs": DataQualityLog,
    "licensing_costs": LicensingCost,
    "model_logs": ModelLog,
    "monitoring_runs": MonitoringRun,
    "monitoring_model_accuracy": MonitoringModelAccuracy,
}


def _counts(session: Session) -> dict[str, int]:
    return {
        name: session.exec(select(func.count()).select_from(model)).one()
        for name, model in TABLES.items()
    }


def counts() -> dict[str, int]:
    """Return row counts for all application tables without returning row data."""
    return run(_counts)
