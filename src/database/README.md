# Recommendation database

The public API is organized by table and owns its transactions. Teammates pass
normal Python values; each function validates them through `schemas.py`, commits,
and returns a loaded record:

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

A cleaner can return a dictionary and unpack it into the relevant function:

```python
normalized_user = {"user_id": "42", "age": 31}
users.create(**normalized_user)
```

For a Kafka poll batch, normalize each message to the canonical event fields and
write the batch in one transaction. Deterministic event UUIDs make replay safe:

```python
from src.database.interactions import stream_events

normalized_events = [normalize_message(message) for message in kafka_batch]
inserted_count = stream_events.create_many(normalized_events)
```

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
also derives `user_movie_history`, uses deterministic event IDs, and is safe to
rerun. Live services and Kafka consumers must use the interaction API instead.

```sh
docker compose --profile tools run --rm database-roll-up
```

The command accepts `--data-dir` and `--chunk-size` when run directly.

## Run and administer PostgreSQL

Copy `.env.example` to `.env`, set `POSTGRES_PASSWORD`, then run:

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
