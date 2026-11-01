# Database

The database is organized by table and table-specific interactions. There are 6 
    

Code is modular, so pass
normal Python values, and each function validates them through `schemas.py`, commits,
and then returns either a loaded record or the missing data.

For example:

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

Ideal use case, is we should normalize data and then unpack it into the relevant function, e.g.:

```python
normalized_user = {"user_id": "42", "age": 31}
users.create(**normalized_user)
```

For a normalized user or movie batch, insert only missing source records and
inspect IDs that were deliberately left unchanged:

```python
skipped_user_ids = users.create_missing_many(normalized_users)
```

Invalid or duplicate input fails the whole batch. Existing records are returned in
input order and must be changed through the explicit `update()` operation.

For a Kafka poll batch, normalize each message to the canonical event fields and
write the batch in one transaction. Deterministic event UUIDs make replay safe:

```python
from src.database.interactions import stream_events

normalized_events = [normalize_message(message) for message in kafka_batch]
inserted_count = stream_events.create_many(normalized_events)
```

Immutable interaction records can be searched in bounded pages. For a daily
incremental job, use the stream event storage time so late activity enters the
next storage-day batch while retaining its original `event_time`:

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
or dead-letter an input:

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

## Transactions and initial data

Session-aware implementations live as private underscore functions beside each
public table API. Cross-table transactions are exposed as purpose-specific public
operations, such as `cold_start_profiles.complete_generation`; teammates should
never orchestrate sessions themselves.

Queue workers use `cold_start_jobs.claim()` to lease work, `retry()` after a
recoverable failure, and `cold_start_profiles.complete_generation()` after success.
The last operation saves the profile and removes the queue job atomically.

`roll_up.py` is exclusively for the initial users, movies, and events snapshot. It
inserts missing users and movies without replacing existing source data, derives
`user_movie_history`, uses deterministic event IDs, and is safe to rerun. Its JSON
report includes created counts and skipped IDs. Live services and Kafka consumers
must use the interaction API instead.

```sh
docker compose --profile tools run --rm database-roll-up
```

The command accepts `--data-dir` and `--chunk-size` when run directly.

## Running in Production

In `.env`, set `POSTGRES_PASSWORD` to be a secure password, then run:

```sh
docker compose up -d database database-init
docker compose ps
```

PostgreSQL is available only on the Compose network. Use the container for a SQL
shell or logical backup:

```sh
docker compose exec database psql -U recommender -d recommender
docker compose exec -T database pg_dump -U recommender -d recommender > backup.sql
```

Run focused tests with `python -m unittest discover -s src/database/tests -v`.
`initialize.py` uses `create_all` only as a pre-production bootstrap; add Alembic
migrations before production schema evolution.
