"""The supported teammate-facing database API.

Import the module matching the table you need; public calls validate their named
inputs, open and commit a transaction, and return readable records::

    from src.database.interactions import stream_events, users
    user = users.create(user_id="42", age=31)
    stream_events.create(
        user_id=user.user_id,
        event_type="watch",
        movie_id="twelve+monkeys+1995",
        event_time="2025-01-01T00:00:00Z",
    )

Use ``create_many``/``upsert_many`` for a normalized Kafka or backfill batch.
Append-only modules expose no update operation. Functions prefixed with an
underscore are private transaction implementations and must not be imported by
application code. Validation errors describe bad inputs, database integrity errors
describe constraint violations, and ``RecordNotFoundError`` identifies updates of
missing records.
"""

from src.database.interactions import (
    cold_start_jobs,
    cold_start_profiles,
    llm_usage,
    movies,
    recommendation_serves,
    stream_events,
    user_movie_history,
    users,
)
from src.database.interactions._shared import RecordNotFoundError

__all__ = [
    "RecordNotFoundError",
    "cold_start_jobs",
    "cold_start_profiles",
    "llm_usage",
    "movies",
    "recommendation_serves",
    "stream_events",
    "user_movie_history",
    "users",
]
