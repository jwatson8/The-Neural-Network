# Monitoring

The monitoring worker periodically calculates live operational metrics and saves
each completed evaluation for Grafana.

## Workflow

```text
monitoring_worker
    -> workload
    -> licensing, LLM, and recommendation-quality calculations
    -> MonitorResult
    -> monitoring_runs
    -> Grafana
```

`monitoring_worker.py` runs one workload immediately, then repeats using the
`MonitoringConfig.interval_seconds` start-to-start cadence. A single worker
process never overlaps workloads and continues after failures.

`workload.py` creates one shared half-open evaluation window, runs every
calculator, builds a `MonitorResult`, and saves it through the
`monitoring_runs` database interaction. Persistence happens only after every
calculation succeeds, so partial runs are not stored.

The calculators under `calculations/` query and paginate only the records they
need:

- `license_fees.py` summarizes licensing charges.
- `llm_costs.py` summarizes token usage and estimated LLM cost.
- `live_model_accuracy.py` attributes rating events to recent recommendations.

Successful evaluations are appended to `monitoring_runs`. Failures are written
to container logs and, when PostgreSQL is available, saved as sanitized
`monitoring_run_failure` records in `data_quality_logs`.

## Running

Run only the worker and its database dependencies:

```sh
docker compose --profile monitoring up -d --build monitoring-worker
docker compose logs -f monitoring-worker
```

Run the complete monitoring and Grafana profile:

```sh
docker compose --profile monitoring up -d --build
```

Grafana setup and access instructions are in
[`grafana/README.md`](grafana/README.md).

## Tests

```sh
python -m pytest src/monitoring/tests
```