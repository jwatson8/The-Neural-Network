# Monitoring

The monitoring worker calculates independent five-minute UTC buckets and saves
them for Grafana. Grafana derives rolling 24-hour views from those buckets.

## Workflow

```text
monitoring_worker
    -> workload
    -> licensing, LLM, and recommendation-quality calculations
    -> five-minute MonitorResult buckets
    -> monitoring_runs
    -> Grafana
```

`monitoring_worker.py` runs one workload immediately, then repeats on five-minute
UTC boundaries. A single worker process never overlaps workloads and continues
after failures.

`workload.py` reconciles the preceding hour and fills missing buckets from the
preceding 24 hours. It reads each enclosing source range once, builds one
`MonitorResult` per half-open bucket, and atomically upserts the batch. Repeated
runs update deterministic bucket records instead of appending duplicate totals.

The calculators under `calculations/` query and paginate only the records they
need:

- `license_fees.py` summarizes licensing charges.
- `llm_costs.py` summarizes token usage and estimated LLM cost.
- `live_model_accuracy.py` attributes rating events through bounded, indexed
  user/time lookups and JSONB movie membership.

Successful buckets are inserted or updated in `monitoring_runs`. Events arriving
within the one-hour reconciliation period update their original bucket. Failures
are written to container logs and, when PostgreSQL is available, saved as
sanitized `monitoring_run_failure` records in `data_quality_logs`.

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
