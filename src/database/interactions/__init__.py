"""The supported import-facing database API.

Import the module matching the table you need; public calls validate their named
inputs, open and commit a transaction, and return readable records::
EX:
    from src.database.interactions import stream_events, users
    user = users.create(user_id="42", age=31)
    stream_events.create(
        user_id=user.user_id,
        event_type="watch",
        movie_id="twelve+monkeys+1995",
        event_time="2025-01-01T00:00:00Z",
    )

Use ``create_many`` for immutable event batches and ``create_missing_many`` when
loading users or movies without replacing records already present. User/movie
history is derived atomically from immutable watch and rating events. Append-only
modules expose no update operation and provide cursor-based ``search`` functions
for bounded offline reads. Functions prefixed with an underscore are private
transaction implementations and must not be imported by application code. Validation
errors describe bad inputs, database integrity errors describe constraint violations,
and ``RecordNotFoundError`` identifies updates of missing records.

Watch events also derive immutable licensing charges in the same transaction. Read
those snapshots through ``licensing_costs``; callers do not create them directly.
"""

from src.database.interactions import (
    cold_start_jobs,
    cold_start_profiles,
    data_quality_logs,
    licensing_costs,
    llm_usage,
    model_logs,
    monitoring_runs,
    movies,
    recommendation_serves,
    stream_events,
    user_movie_history,
    users,
)
from src.database.interactions._pagination import SearchCursor, SearchPage
from src.database.interactions._shared import RecordNotFoundError

__all__ = [
    "RecordNotFoundError",
    "SearchCursor",
    "SearchPage",
    "cold_start_jobs",
    "cold_start_profiles",
    "data_quality_logs",
    "licensing_costs",
    "llm_usage",
    "model_logs",
    "monitoring_runs",
    "movies",
    "recommendation_serves",
    "stream_events",
    "user_movie_history",
    "users",
]
