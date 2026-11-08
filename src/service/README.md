# Recommendation service

Run from the repository root. The serving entry point is `src.service.api:app`.
See [the deployment guide](../../deployment/README.md) for the VM instructions.

```sh
python -m pip install -r src/requirements-service.txt
python -m src.model.prepare
python -m flask --app src.service.api run --host 127.0.0.1 --port 8082
```

Production uses Gunicorn in Docker. The API loads `artifacts/model.joblib` once,
returns plain comma-separated catalog IDs, and uses PostgreSQL interactions for
saved LLM profiles, pending jobs, stream events, recommendation audits, LLM usage,
and captured course requests. Warm users use SVD; users with saved profiles use the
trained TF-IDF features; users awaiting profiles receive popularity recommendations.

`GET /database/summary` is public and returns row counts for each application table.
`GET /database/cold-start-jobs?status=pending&offset=0&limit=50` returns paginated
job status, attempts, and timing information (maximum 100 rows per page). These
read-only endpoints omit user IDs, profile text, raw Kafka lines, and error details.

`GET /recommend/<user_id>` validates the route value and query parameters before
inference, then validates the generated comma-separated response. Invalid requests
return 400; invalid generated output returns 500. Validation logs are sanitized
and do not include raw request or response values.

The live Compose profile adds `src.service.profile_worker` and
`src.service.course_stream`. Kafka account-created events are persisted to
PostgreSQL `stream_events` as `account_created` records and enqueue profile jobs
only when no profile is already saved;
recommendation requests are captured separately as course-request evidence.
Validation failures are logged without raw input. Database errors escape before
the consumer commits the polled batch, allowing Kafka redelivery after restart;
there is no in-process retry/backoff loop for these errors. User API and LLM calls
run only in the worker. The worker validates preference fields and structured LLM
output, persists results, and retries failures. The live User API mapping still
depends on confirming its identity/field contract. No service activation is implied
until real connections and credentials are configured. Empty descriptions
deliberately use popularity.

Alvajoy's Flask application factory and route split are retained. The request path
does not reload the movie catalog or transform every movie on each cold request.
Raw descriptions do not bypass the LLM or prevent queueing. Initial users, movies,
and events can be loaded idempotently into PostgreSQL with `src.database.roll_up`.

Tests: `python -m unittest discover -s src/tests -v`.
