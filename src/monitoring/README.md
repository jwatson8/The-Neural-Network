# Monitoring

The monitoring worker calculates independent five-minute UTC buckets and saves
them for Grafana. Grafana derives rolling 24-hour views from those buckets.

## Workflow

```text
monitoring_worker
    -> workload
    -> licensing, LLM, recommendation-quality, request-latency, drift,
       availability, and Kafka-rejection calculations
    -> five-minute MonitorResult buckets
    -> monitoring_runs + per-route child rows
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
- `request_latency.py` calculates successful-request counts, average latency,
  p50/p95/p99, maximum latency, and the count strictly over 600 ms.
- `data_drift.py` compares rolling one-hour rating and event-type distributions
  from per-bucket counts aggregated in PostgreSQL.
- `availability.py` counts course-logged recommendation requests by outcome:
  status 200 (success), status 0 (no response: timeout or connection failure),
  and any other status. Availability is `success / requests`.

- `rejections.py` counts Kafka course-log records rejected by the consumer,
  one count per `rejected_requests` reason, plus the total, against every
  record the consumer handled in the bucket.

Kafka rejections are bucketed by when the consumer handled the record, because
rejected lines often have no usable timestamp. The denominator,
`kafka_record_count`, is rebuilt from `stream_events` (by `stored_at`),
`course_requests` and `rejected_requests` (by `received_at`), since the consumer
stores no per-poll total. Grafana's **Kafka Rejections** dashboard graphs the
per-reason counts and the rejection rate. Alerts use the rate over the last
three completed buckets, only when they hold at least 100 records, held for 5
minutes: a warning between 10% and 20%, critical above 20%.

Availability uses `course_requests`, the course client's own log of every
request, because `recommendation_serves` only records requests the API answered
and so cannot count failures. Requests are bucketed by the UTC timestamp in the
logged line, not by ingestion time. Buckets computed before availability existed
keep zero counts, which Grafana shows as null rather than 0% availability.

Successful buckets are inserted or updated in `monitoring_runs`. Events arriving
within the one-hour reconciliation period update their original bucket. Failures
are written to container logs and, when PostgreSQL is available, saved as
sanitized `monitoring_run_failure` records in `data_quality_logs`.

Recommendation quality is also stored in `monitoring_model_accuracy`, with one
child row for every route and model version active in the attribution window.
Ratings can credit multiple route/version pairs when each independently
recommended the rated movie. Routes with serves but no attributed ratings retain
a zero sample count and null accuracy.

Request latency is stored service-wide on `monitoring_runs` and by route in
`monitoring_route_latency`. Completed buckets with no successful requests retain
zero counts and null latency statistics, allowing Grafana to distinguish zero
traffic from a missing monitoring bucket.

The additive columns (including the availability and Kafka-rejection counts),
constraints, drift placeholders, and route-latency table are
owned by `src.database.migrations.add_monitoring_schema`; they are intentionally
separate from the experiment migration.

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
