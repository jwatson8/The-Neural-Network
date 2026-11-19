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

## Experiment Serving

Experimentation defaults to `EXPERIMENTS_ENABLED=False` in the Flask factory.
Compose and environment variables do not enable it. Enabling requires one explicit
`EXPERIMENT_ID` UUID in the factory configuration; missing IDs or lists are rejected.
The Manager reads that experiment and checks lifecycle, time windows, traffic and
sticky assignments. Other active experiments are never discovered or selected.

`create_app(..., experiment_manager=None, treatment_server=None,
experiment_eligibility=None)` accepts injectable dependencies. The optional
eligibility callback receives a `ServingRequest` and must return a boolean; the
default admits all validated users. Existing users are ensured before eligible
assignment decisions. A callback/Manager failure uses normal serving without an
experimental exposure. A persistent general database outage still returns 503
through the existing audit/history handling. Setting the app's enable flag false
also stops decisions for an app constructed with experimentation enabled.

Control is the existing SVD/profile/popularity pipeline, declared by
`CONTROL_SERVING_POLICY` (default `legacy`). Its version is the loaded bundle's
embedded version or the existing configured `MODEL_VERSION`; unknown versions
cannot establish an exposure. A matching Control policy includes the pipeline's
normal cold-start/popularity paths. Their actual routes remain unchanged.

`service.experiment_routing` defines the hosting boundary:

```python
TreatmentServer(request: ServingRequest, decision: ExperimentDecision) -> ServingResult
RecommendationRouter.serve(request, decision, control) -> ServingResult
```

The request includes user ID, immutable excluded movie IDs and `k=20`. The trusted
hosting adapter must report recommendation IDs, actual model version, actual route,
actual policy, fallback path, and whether the request was fulfilled. It must run the requested policy/model pin rather than echoing decision metadata.
Optional `policy_version` distinguishes a policy/configuration version from the
actual `model_version`; older adapters retain their model-version matching contract.
Multi-model hosting is not supplied. Alvajoy can inject an adapter at app construction
without changing the Flask request path.

Absent Treatment, inference exceptions, invalid/empty/duplicate/seen outputs,
unfulfilled requests, fallback metadata, or policy/version mismatches all use the
legacy pipeline and produce no Treatment exposure. A Control mismatch likewise
uses its actual version/route without an exposure. Control inference failures keep
the existing 500/503 behavior. HTTP output stays plain comma-separated IDs.

## Exposure Attribution

Assignment and exposure are distinct. The Manager commits assignment before
inference. One successful recommendation audit is then written per request, with
`experiment_assignment_id` only when the assigned policy/version was fulfilled
and the final response passed validation. The interaction verifies the assignment
exists and belongs to the user. Failed inference/output creates no serve; audit
failure returns 503 and rolls back. Failed/unsupported Treatment assignments remain
available for intention-to-treat analysis, while fallback serves have a NULL FK.

Serve rows retain actual route/version and IDs. Structured `experiment_serve` logs
add request ID, intended assignment/variant, valid exposure assignment, actual
policy, fallback path and decision reason. Policy/fallback fields have no dedicated
database columns; retain these logs if detailed fallback analysis is needed.

Exposure here means a validated server-side 200 response with a committed audit,
not acknowledged client receipt. Audit must commit before returning so failures
can still return 503; WSGI cannot prove delivery or roll back a client disconnect.
In-flight requests may finish after pause/expiry. No schema changes are introduced.

Kafka watch/rating events and licensing charges do not carry serve/assignment IDs.
User-level intention-to-treat joins must use assignment time and experiment windows,
including users with no exposure. Event-level joins still require a predefined
attribution window/rule; existing rating attribution can credit multiple models.
No client delivery, engagement, licensing savings or statistical confidence is
inferred from a successful serve alone.

Focused offline tests use real isolated assignment/audit persistence and injected
stub serving, with no live hosting or production experiment data:

```sh
python -m pytest src/tests/test_experiment_api.py src/tests/test_service.py
```


## Cost-aware Treatment

The optional built-in `CostAwareTreatmentServer` expands candidates from the same
SVD, saved-profile, and popularity paths, then applies a catalog-license-cost
penalty. Both experimentation and `COST_AWARE_TREATMENT_ENABLED` remain off by
default. Registration alone does not activate or create an experiment.

Use policy `cost_aware_rerank` and generate its version with
`treatment_version(actual_model_version, CostAwareConfig(...))`. The actual bundle
version remains in recommendation audits; policy/configuration version and the
underlying route are recorded separately in structured logs. Reference ranking
prices come from `movies`, not the observed `licensing_costs` ledger.

See [Cost-aware Reranking](../experimentation/cost_aware_reranking.md) for the
scoring equation, conservative lambda default, bounded pool/lookup behavior,
missing-price rules, injection contract, diagnostic command and limitations.
