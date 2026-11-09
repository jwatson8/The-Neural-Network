# Online evaluation evidence

## Production results: pending

No verified production measurement window was available when this section was written. Do not submit synthetic test values as live model performance. Replace this status with a dated export or Grafana screenshot and the aggregate results below once telemetry is available.

The PostgreSQL service integration is now merged in PR #43. Confirm that this version is deployed and that all four input tables (`stream_events`, `recommendation_serves`, `licensing_costs`, `llm_usage`) receive fresh production data. Merely starting a healthy monitoring worker can produce empty buckets, not valid evidence of zero activity.

The monitoring services also require the Compose `monitoring` profile; the ordinary deployment workflow does not enable that profile. Follow the [monitoring setup](../../src/monitoring/README.md) and [Grafana access guide](../../src/monitoring/grafana/README.md). The dashboard is **Monitoring / Monitoring Overview**. Record the time range and sample counts with each screenshot.

## Collect an aggregate on the VM

After monitoring and ingestion are working, run this read-only query from `/home/the-neural-network`. It summarizes the **previous completed UTC calendar day**. This differs from the daily trainer's Eastern calendar day; label the window explicitly when reporting results. It exposes aggregate counts and costs, not user records or secrets.

```bash
cd /home/the-neural-network
docker compose exec -T database sh -c 'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
WITH bounds AS (
  SELECT (date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') AS finish
), selected AS (
  SELECT m.* FROM monitoring_runs m CROSS JOIN bounds b
  WHERE m.window_start >= b.finish - INTERVAL '24 hours'
    AND m.window_end <= b.finish
    AND m.window_end = m.window_start + INTERVAL '5 minutes'
)
SELECT
  (SELECT finish - INTERVAL '24 hours' FROM bounds) AS start_utc,
  (SELECT finish FROM bounds) AS end_utc,
  COUNT(*) AS buckets_present,
  COUNT(DISTINCT window_start) AS distinct_bucket_starts,
  288 AS expected_buckets,
  MAX(generated_at) AS last_bucket_generation,
  SUM(evaluated_rating_count) AS evaluated_ratings,
  SUM(attributed_rating_count) AS attributed_ratings,
  SUM(positive_rating_count) AS positive_ratings,
  SUM(positive_rating_count)::numeric / NULLIF(SUM(attributed_rating_count), 0) AS positive_rating_fraction,
  SUM(attributed_rating_count)::numeric / NULLIF(SUM(evaluated_rating_count), 0) AS attribution_coverage,
  SUM(licensing_charge_count) AS license_charges,
  SUM(licensing_priced_charge_count) AS priced_license_charges,
  CASE WHEN SUM(licensing_priced_charge_count) > 0 THEN SUM(total_license_cost) END AS known_license_cost,
  SUM(llm_request_count) AS llm_requests,
  SUM(llm_priced_request_count) AS priced_llm_requests,
  SUM(llm_input_tokens) AS llm_input_tokens,
  SUM(llm_output_tokens) AS llm_output_tokens,
  CASE WHEN SUM(llm_priced_request_count) > 0 THEN SUM(llm_estimated_cost_usd) END AS known_llm_cost_usd
FROM selected;
SQL
```

Keep the output with its collection timestamp and deployed commit/model version. Check that bucket counts are 288 with no duplicates, timestamps are current, and telemetry is arriving; bucket coverage alone does not prove ingestion completeness. A blank accuracy means no attributed ratings. Costs are partial when priced counts are below total counts. Summed input/output tokens describe recorded usage, not necessarily all requests. Do not average bucket accuracies or claim zero costs from missing data. Record monitoring failures and any known telemetry gaps alongside the result.

If the query returns no useful observations, fix/start ingestion and monitoring, then collect a later window. No database reset or deletion is required.

## Calculator validation (synthetic, not production)

The repository's [monitoring tests](https://github.com/jwatson8/The-Neural-Network/tree/main/src/monitoring/tests) verify these examples:

Validation during this write-up: `python -m pytest src/monitoring/tests -q -o addopts=''` completed with **22 tests and 6 subtests passing**. The production SQL query above has not been run against the VM.

| Metric | Synthetic inputs | Expected result |
|---|---|---|
| Positive-rating fraction | First bucket: attributed ratings 8 and 4; unattributed rating 10 | 1/2 = 50%; the unattributed rating is excluded |
| Second accuracy bucket | One attributed rating of 9 | 1/1 = 100%; combined counts across both buckets give 2/3, not 75% |
| License cost | First bucket: one known charge 1.25 and one missing price | Known cost 1.25; 1 priced / 2 charges |
| LLM usage | First bucket: 100+50 input tokens, 20+10 output tokens; one known cost 0.01 | 150 input, 30 output; known cost 0.01; 1 priced / 2 requests |

These validate calculation behavior only. They do **not** satisfy the assignment's requirement for production evaluation results.
