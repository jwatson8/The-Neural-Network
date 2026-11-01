# Recommendation Database

## Tech Stack

The database uses PostgreSQL, with functionality provided by SQLModel, SQLAlchemy,
and Psycopg.

## Organization

The database is organized by table and table-specific interactions. There are 8 tables:

`stream_events`, `user_movie_history`, `recommendation_serves`, `users`, `llm_usage`, `movies`, `cold_start_jobs`, and `cold_start_profiles`.

Each table has a corresponding module under `src/database/interactions`. Public
functions validate inputs through `schemas.py`, own their database transaction, and
commit on success or roll back on failure.

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
function returns the number of newly inserted events.

## Search Functionality

The append-only tables are `stream_events`, `recommendation_serves`, and
`llm_usage`.

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

LLM usage and recommendation serves provide the same cursor pattern using their
occurrence and serve timestamps. Search boundaries must include a timezone; the
start is inclusive and the end is exclusive. Reuse the same window and filters when
passing a cursor to the next search call.

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
`user_movie_history`, uses deterministic event IDs, and is safe to rerun. Its JSON
report includes created counts and skipped IDs. Live services and Kafka consumers
must use the interaction API instead.

```sh
docker compose --profile tools run --rm database-roll-up
```

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

## Left to Add

Flask currently uses SQLite for the cold-start workflow. The PostgreSQL
`cold_start_jobs` and `cold_start_profiles` interactions are implemented but have not
yet been integrated into the Flask service.
