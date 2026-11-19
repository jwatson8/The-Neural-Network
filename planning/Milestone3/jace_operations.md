# Model switching, provenance, and scheduled updates

## Architecture and switching

Question: how can two model versions serve requests while the default changes?

```text
HTTP :8082 -> Flask request handler -> experiment router
                                     | control pin -> model A
                                     | treatment pin -> model B + cost-aware policy
                                     | no assignment -> current default
mounted artifacts/models/<sha>.joblib -> background loader -> atomic default swap
                     request snapshot retains its original model until it finishes
```

We reuse the existing `RecommendationRouter` and `ModelRegistry`. Two registered
versions can be pinned to experiment arms with `CONTROL_MODEL_VERSION` and
`TREATMENT_MODEL_VERSION`. This is an in-process router, not two independent API
containers. One Gunicorn worker with four threads keeps requests and activation
acknowledgements consistent. The loader validates and warms the candidate before
publishing a new immutable serving snapshot. Existing requests finish with their
old handle; experiment pins remain fixed. A rejected model leaves the old model
serving. This supports model switching without API restart; a general code or VM
deployment can still interrupt service and is not claimed to be zero downtime.

Models are outside the image in the VM's `artifacts` directory, mounted read-only
into the API at `/inputs`. The image contains code and the original course data,
not the production model. Models and their registrations are retained by checksum.
The trade-off is simplicity versus one API process being a shared failure point.

Implementation: [registry](../../src/service/model_registry.py),
[router](../../src/service/experiment_routing.py), [Compose](../../compose.yaml),
[activation and rollback](../../deployment/daily_model.sh).

## Provenance

Every successful prediction is stored in PostgreSQL `recommendation_serves` with
its actual `model_version` (our model-version ID), request UUID and timestamp.
The log also names `model_version_id` and the artifact checksum. HTTP headers
`X-Request-ID`, `X-Model-Version-ID`, and `X-Model-SHA256` make a request traceable
without changing the required comma-separated response body.

New training records the Git commit baked into the Docker image, source checksum,
library versions, training cutoff, and training-data manifest. Original CSVs are
retained under `artifacts/training-data/<sha>/`; the frozen PostgreSQL JSON is
retained under `artifacts/daily/<day>/snapshot.json.gz`. Its checksum is over the
uncompressed JSON. Registration maps the version to immutable model bytes and
these records. Keep the artifacts volume backed up and restrict its access: the
training snapshot contains user data. Older models without recorded Git provenance
cannot acquire a historical training commit just by deploying newer code.

To trace a request and verify its model and data checksums on the VM:

```bash
cd /home/the-neural-network
docker compose exec -T api python -m src.model_updates.trace REQUEST_UUID
```

Implementation: [training provenance](../../src/model_updates/provenance.py),
[daily training](../../src/model_updates/daily.py),
[trace command](../../src/model_updates/trace.py), [prediction logging](../../src/service/api.py).

## Scheduling and evidence

The [scheduled workflow](../../.github/workflows/daily-model.yml) checks hourly.
`deployment/scheduled_update.py` stores the previous attempt in
`state/scheduled-update.json` and permits the next run after 36 hours (configurable
36–48). GitHub delays can make the actual interval longer. The old independent
02:00 trainer is disabled to avoid duplicate schedules. Repository variables are
`AUTOMATIC_MODEL_PROMOTION_ENABLED=true`, `MODEL_UPDATE_INTERVAL_HOURS=36`, and
optional `MODEL_UPDATE_START_AT` as a timezone-aware ISO timestamp. A failed run
is recorded and requires investigation; it is not counted as an update.

For the November 22, 11:59 p.m. Eastern submission, an anchor of
`2026-11-20T06:00:00Z` allows runs around Friday 1 a.m. and Saturday 1 p.m. Eastern,
both inside the required 72-hour window. These are planned times, not evidence
that either model has been promoted. The existing statistical promotion gates
remain in force unless a separately documented refresh policy is selected.

Use **Actions → Model operations and evidence → Run workflow → status** to read
aggregate reports. `update` with a completed day runs evaluation and possible
activation immediately; it does not bypass quality checks. Local continuity
reports record success/failure counts, latency, observed model versions, and API
container identity around each update. `promotion.json` records the completed
activation time. A green run that retains the old model is not a production
update. Add two actual promotions and course Kafka availability evidence to the
final report; CI synthetic model swaps demonstrate mechanics only.

## Deployment verification

CI tests streamed deployment input, stale consumer images, model validation,
concurrent serving snapshots, rollback, scheduling, and retained-data checksums.
Docker CI drives HTTP requests during a synthetic promotion and rollback. Manual
CD deploys the exact tested commit and verifies that the API, updater, cold-start
worker, tunnel, and Kafka consumer run its image, followed by an HTTP check.
The earlier stdin-consumption fix remains in place.
