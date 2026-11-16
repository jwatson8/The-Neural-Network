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

SVD is used only when the loaded model includes interactions with at least
**10 distinct movies** for the user. Repeated video minutes, repeat ratings, and
watching then rating the same movie count as one movie; account creation does not
count. The request also reads `user_movie_history` to exclude movies seen since the
model was trained. New history does not update SVD factors: users below the trained
threshold use a saved profile or popularity fallback until retraining and promotion.

`GET /database/summary` is public and returns row counts for each application table.
`GET /database/cold-start-jobs?status=pending&offset=0&limit=50` returns paginated
job status, attempts, and timing information (maximum 100 rows per page). These
read-only endpoints omit user IDs, profile text, raw Kafka lines, and error details.

`GET /recommend/<user_id>` validates the route value and query parameters before
inference, then validates the generated comma-separated response. Invalid requests
return 400; invalid generated output returns 500. Validation logs are sanitized
and do not include raw request or response values.

The live Compose profile adds `src.service.profile_worker`, an SSH tunnel
sidecar, and `src.service.course_stream`. The consumer shares the tunnel's
network namespace and reaches Kafka through `127.0.0.1:9092`; Kafka SASL
authentication remains configured independently. Kafka account-created events are persisted to
PostgreSQL `stream_events` as `account_created` records and enqueue profile jobs
only when no profile is already saved;
recommendation requests are captured separately as course-request evidence.
Watch and rating events use the course stream formats
`time,user_id,GET /data/m/<movie_id>/<minute>.mpg` and
`time,user_id,GET /rate/<movie_id>=<rating>`.
The referenced movie must already exist in the catalog. Each record is keyed by
its Kafka topic, partition, and offset for replay-safe persistence; accepted watch
and rating events update `user_movie_history`, and watches also create licensing
charges.
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
