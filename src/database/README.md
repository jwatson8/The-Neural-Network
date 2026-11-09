# Recommendation Database

## Tech Stack

The database uses PostgreSQL, with functionality provided by SQLModel, SQLAlchemy,
and Psycopg.

## Organization

The database is organized by table and table-specific interactions. There are 13 tables:

`stream_events`, `user_movie_history`, `recommendation_serves`, `users`,
`llm_usage`, `movies`, `cold_start_jobs`, `cold_start_profiles`,
`data_quality_logs`, `model_logs`, `licensing_costs`, `monitoring_runs`, and
`monitoring_model_accuracy`.

Tables have a corresponding module under `src/database/interactions`, except
`monitoring_model_accuracy`, whose child rows are managed atomically through the
`monitoring_runs` module. Public functions validate inputs through `schemas.py`, own
their database transaction, and commit on success or roll back on failure.

## Using the Interaction API

Import table interaction modules from the central `src.database.interactions`
package. For example:

```python
from src.database.interactions import stream_events, users

user = users.create(user_id="42", age=31, occupation="programmer")
event = stream_events.create(
    user_id=user.user_id,
    event_type="watch",
    movie_id="twelve+monkeys+1995",
    event_time="2025-01-01T00:00:00Z",
)
```

This example assumes the referenced movie already exists in the catalog.

Normalize incoming data, then unpack it into the relevant function:

```python
normalized_user = {"user_id": "42", "age": 31}
users.create(**normalized_user)
```

There are also batch operations. For example:

```python
skipped_user_ids = users.create_missing_many([
    {"user_id": "42", "age": 31, "occupation": "programmer"},
    {"user_id": "43", "age": 27, "occupation": "designer"},
])
```

In this example, `skipped_user_ids` contains IDs that already existed and were left
unchanged. An empty list means both users were created. Invalid or duplicate input
fails the entire batch before any records are written.

For replay-safe event ingestion, normalize a Kafka poll batch and provide stable
event UUIDs:

```python
inserted_count = stream_events.create_many(normalized_events)
```

The batch is written in one transaction. Duplicate event UUIDs are skipped, and the
function returns the number of newly inserted events. Newly inserted watch and
rating events update `user_movie_history` in the same transaction. Application
callers write through `stream_events`; `user_movie_history` exposes read operations.

Each history row records `last_recorded_watch_at`, `last_recorded_rating_at`, the
latest rating, and whether the most recent accepted rating changed its predecessor.
Older and equal-time events remain in `stream_events` but do not regress the
materialized history row.

The course Kafka consumer routes account-created events through the service store
to `src.data_quality.ingestion.persist_stream_event`, which delegates validation
to `stream_events.create_many`. Event IDs are stable for a Kafka topic/partition/
offset, so replayed records are deduplicated. Invalid records produce sanitized
validation logs. Database errors are not converted to validation failures; they
propagate before the consumer commits its polled batch, leaving it available for
Kafka redelivery after restart. Recommendation-request lines are stored separately
in `course_requests`, not as interaction events.

New watch events also derive immutable licensing charges in the same transaction.
The first watch for a user/movie pair is charged. Later watches are charged only
when their event time is at least 24 hours after the preceding accepted watch.

## Search Functionality

The append-only tables are `stream_events`, `course_requests`, `licensing_costs`,
`recommendation_serves`, `llm_usage`, `data_quality_logs`, `model_logs`, and
`monitoring_runs`.

Immutable interaction records can be searched in bounded pages. One intended use is
a daily incremental job. In that case, use the stream event storage time so late
activity enters the next storage-day batch while retaining its original `event_time`.

For example:

```python
from datetime import UTC, datetime

start = datetime(2026, 11, 1, tzinfo=UTC)
end = datetime(2026, 11, 2, tzinfo=UTC)
cursor = None

while True:
    page = stream_events.search(
        start=start,
        end=end,
        time_basis="stored_at",
        cursor=cursor,
        limit=1000,
    )
    process(page.records)
    if page.next_cursor is None:
        break
    cursor = page.next_cursor
```

LLM usage, recommendation serves, data-quality logs, and model logs provide the
same cursor pattern using their respective timestamps. Search boundaries must
include a timezone; the start is inclusive and the end is exclusive. Reuse the
same window and filters when passing a cursor to the next search call.

Model training and test results are recorded independently from data-quality
findings. Training logs identify the source table, storage-time window, row count,
and optional artifact size. Test logs identify the test and store its named metrics
as a JSON object.

Updates raise a named error when the target does not exist. Validation and database
constraint errors remain distinct so callers can choose whether to reject, retry,
or dead-letter an input. Example:

```python
from src.database.interactions import RecordNotFoundError, users

try:
    users.update("42", occupation="programmer")
except RecordNotFoundError:
    users.create(user_id="42", occupation="programmer")
```

Every public function in `interactions/` documents its inputs, result, and behavior
for editor hover help. Application and automation code should import these table
modules from `src.database.interactions`; it should not import SQLModel tables or
functions whose names begin with an underscore.

## Initial Data Roll Up

The system requires initial data, which is provided by the existing CSV snapshots.

`roll_up.py` is exclusively for the initial users, movies, and events snapshot. It
inserts missing users and movies without replacing existing source data, derives
`user_movie_history` and `licensing_costs`, uses deterministic event IDs, and is
safe to rerun. Its JSON report includes created counts and skipped IDs. Live
services and Kafka consumers must use the interaction API instead.

```sh
docker compose run --rm database-roll-up
```

Normal Compose startup runs this idempotent roll-up before starting the API. Running
the command manually is useful for backfills or verifying a standalone database.

## Running in Production

In `.env`, set `POSTGRES_PASSWORD` to be a secure password, then run:

```sh
docker compose up -d database database-init
docker compose ps
```

## Tests and Schema Changes

Run the focused database tests with:

```sh
python -m unittest discover -s src/database/tests -v
```

`database-init` uses `SQLModel.metadata.create_all()` for development and initial
bootstrap. Use a migration tool such as Alembic before making production schema
changes.

## Service Integration

The Flask API, profile worker, and Kafka consumer use the PostgreSQL interaction
functions for profiles, jobs, recommendation serves, LLM usage, stream events, and
raw course-request evidence. Kafka request rows are deduplicated by topic, partition,
and offset so a replay after a failed commit does not duplicate evidence.
