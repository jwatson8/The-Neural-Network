# Daily model updates

The daily container prepares and evaluates models. An optional **Automatic daily model promotion** GitHub workflow then applies the same fixed gates, records the decision in PostgreSQL, and promotes eligible candidates without teammate approval. Manual promotion remains available.

## Enable automatic promotion once

1. Merge this change and successfully deploy latest `main` with the existing CI/deployment workflow. Do this before enabling automation: it executes the deployed VM code and running image, not an untested checkout.
2. Under repository **Settings > Secrets and variables > Actions > Variables**, create the repository variable `AUTOMATIC_MODEL_PROMOTION_ENABLED` with value `true`. This is not a secret or a VM `.env` setting. Set it to `false` to stop future automatic promotions; it does not interrupt an already-running job.
3. The workflow runs daily at **08:17 UTC (03:17 Eastern standard time / 04:17 Eastern daylight time)**, after the container's 02:00 Eastern training schedule. GitHub may delay scheduled runs. **Run workflow** on `main` can run it sooner without SSH; it always handles yesterday in Eastern time. The runner must stay online. This workflow deliberately has no environment-review gate; restrict repository write access to trusted teammates.

The workflow calls `bash deployment/daily_model.sh auto` from `/home/the-neural-network`. It uses both deployment/training locks, reuses yesterday's saved report or trains if absent, rechecks the fixed metric gates, and records the comparison **before** swapping the model. Database audit failure blocks promotion. `insufficient_data` or rejected candidates are normal successful no-ops; failed evaluations fail the job. Previously promoted or rolled-back dates are not promoted again. Interrupted pending promotions require explicit recovery, not an automatic retry that could overwrite evidence. A failed post-promotion audit makes the job fail without undoing a healthy model; rerunning reconciles the saved outcome with PostgreSQL.

### Where the evidence lives

- `model_logs`: append-only `event_type=test` entries named `daily_comparison_v1` and `daily_deployment_v1`. Their `test_result` JSON contains both models' NDCG/Recall scores, cohort counts, bootstrap improvement bound, p95 latency, drift, hashes, policy version and decision/outcome. Deterministic IDs prevent duplicate entries on retries. These are evaluation/deployment audit tests, not fabricated training rows. Full snapshots and artifacts remain on the VM.
- `licensing_costs`: existing actual-play cost records are summarized for the evaluation day's context, including missing costs. They are not copied into this table or treated as hypothetical candidate costs.
- `monitoring_runs`: existing time buckets supply observed positive-rating fraction and sample counts. Overlapping buckets are flagged and not combined into a rate; missing buckets are not assumed to cover the day. These metrics may combine model versions and **do not determine which candidate wins**.

Use the database interaction `model_logs.search(test_type='daily_comparison_v1')` or query `model_logs` to review decisions. Offline NDCG comparison on identical held-out users/catalog remains the promotion criterion. It is a useful proxy, not an A/B experiment or a guarantee of future user behavior. Scores refer to the evaluation candidate; the promoted artifact is a separately validated final refit including yesterday. The GitHub job exposes only a short decision/outcome; it never uploads raw snapshots.

## After merging and deploying

Run the existing GitHub deployment workflow on `main`. Compose now starts `daily-model-updater` alongside the existing services. It uses the same image as the API, with a 1-CPU/2-GB limit and one numerical-computing thread. There is no Docker socket inside this container. The deployment sets its UID/GID to the deployment account so it can write `artifacts/`.

The scheduler checks once per minute and processes the **previous Eastern calendar day after 02:00 America/New_York**. After a restart it catches up on yesterday only, not every missed day. On the spring DST transition it runs after the missing 02:00 hour (03:00); calendar-day windows still handle 23/25-hour days correctly. Setting `DAILY_MODEL_UPDATES_ENABLED=false` in the VM `.env` and recreating the updater disables scheduling.

```bash
cd /home/the-neural-network
docker compose ps daily-model-updater
docker compose logs --tail=50 daily-model-updater
```

**Data prerequisite:** the PostgreSQL `stream_events`, `users`, and `movies` tables must contain production data. The live Kafka consumer stores account-created events in PostgreSQL `stream_events` and recommendation-request evidence separately in `course_requests`; it does not currently ingest watch/rating events from the course log. The daily job logs its computed event-type and rating drift results to `data_quality_logs` and includes them in the report, but account-created-only data cannot supply rating-drift evidence. Confirm watch/rating ingestion before treating daily reports as complete production evidence. The job reports `insufficient_data` when there are no fresh events; it never claims it trained on new data in that case.

The online report also needs PostgreSQL `recommendation_serves` and `llm_usage`. The evaluator uses only cold-start profiles with `updated_at` before the test day from PostgreSQL `cold_start_profiles`. Missing historical profiles use popularity fallback in offline evaluation; it does not regenerate profiles retroactively or query live APIs. Ensure the ingestion/telemetry work populates these tables before treating the daily report as complete production evidence. Profile serving and persistence use PostgreSQL; this job does not regenerate profiles or query live APIs.

## What the job does

1. Copy the current model; verify its training cutoff precedes the test day. New models carry provenance in their bundle. For the original M0 artifact, a matching `artifacts/model.json` (model checksum plus verified M0 dataset checksums) is required. Recover that matching sidecar from the original training run if missing; do not invent a cutoff or overwrite the live model just to create one.
2. Read PostgreSQL in a single repeatable-read, read-only transaction. Freeze inputs as `snapshot.json.gz`. Reads fail above 500,000 rows per table rather than silently truncating; increase capacity/change to streaming before exceeding this small-project limit. Daily reports/snapshots have no automatic retention cleanup: monitor disk usage and archive reviewed runs explicitly.
3. Combine original course history with accumulated production interactions, normalize timestamps, validate schemas/references and deduplicate exact interaction records. Keep all available history; this is a full refit of the existing SVD, not incremental online learning. Rating weights, watch weight 0.25, 32 factors, random seed and content ranking formulas stay the same.
4. Train an evaluation candidate using only interactions **before yesterday** and, for database rows, only those stored before yesterday. Use the champion's frozen movie catalog/metadata. This prevents test-day labels, late-arriving historical records and newly edited movie metadata from entering evaluation training. Evaluate both models on yesterday. Users first observed during the test day are cold-start cases.
5. If checks pass, train a separate final refit on data with event time before the end of yesterday, including events that arrived by snapshot time. That refit can include new catalog entries with metadata. Save it for approval. **The comparison score belongs to the evaluation candidate, not this final refit.**

## Measures and decision

**Offline metric:** macro-average binary NDCG@20 (primary) and Recall@20, also split into warm/cold according to champion interaction history. **Data:** yesterday's final rating per user/movie; ratings >=7 are relevant. Pairs outside the common evaluation catalog or already seen before the day are excluded and counted. **Operationalization:** use the same eligible users, catalog and pre-day history mask for both models; mask before top-20 selection. NDCG sums `relevant/log2(rank+1)` and divides by the ideal top-20 sum. Recall divides hits by the number of eligible relevant movies. Average per user, not per event. This is a retrospective ranking proxy, not proof of how users would behave under different recommendations; exposure bias remains.

For approval eligibility we require at least **100 evaluated users**, a positive **5th percentile of 1,000 paired user-bootstrap mean NDCG differences** (seed 42), inference p95 below 600ms, and `no_drift` for event-type and rating distributions. Drift compares yesterday with the preceding seven days using the existing module's TVD threshold 0.20/minimum 100 samples per window. Missing drift evidence also blocks promotion. These are explicit conservative starting policies, not statistically calibrated universal constants. Observe daily variation and review them with the team before changing them. Microbenchmarks are not an end-to-end production latency guarantee, and repeated daily comparisons aren't a controlled online experiment.

**Online metric:** fraction of recorded successful recommendation serves followed by a matching watch start within 60 minutes. A watch start means `minute_index=0`, deduplicated by user/movie/time. Attribute to the most recent preceding serve, and count a serve at most once. Read a one-hour tail after midnight so late-day requests get the full window. Report recorded personalized fraction and response p95, observed license costs (sum per start where metadata provides cost), LLM input/output tokens and recorded estimated LLM cost. Missing costs are counted. The serve table lacks failures: failure rate is explicitly unavailable. Missing telemetry is not evidence of zero usage. This observed report is separate from candidate offline scores.

## Read reports and run manually

```bash
# Example date; substitute the day you want to evaluate.
bash deployment/daily_model.sh run 2026-11-07
cat artifacts/daily/2026-11-07/report.json
```

Reports use `insufficient_data`, `retain_champion`, `awaiting_approval`, or `failed`. Each date is idempotent: a completed report (including a recorded failure) is not overwritten or retried endlessly. After fixing a problem, move that date's directory to a uniquely named archive before rerunning. Preserve previous attempts as evidence. For backfills, the champion must still have a training cutoff before the requested day; otherwise evaluation refuses leakage.

Each run retains the frozen database snapshot, original data hashes, source-code hash, champion copy/hash, evaluation artifact, metric definitions/settings, decision reasons and, when eligible, the final candidate/hash. Files under `artifacts/` stay ignored by Git. They contain user data: keep them within the team's access-controlled VM storage, not public CI artifacts. A failed job does not touch the production model; inspect its report `stage`/`error_type`, database availability and container logs.

## Approve, promote, and recover

Review the report and confirm ingestion and telemetry coverage before approving a manual update.

```bash
bash deployment/daily_model.sh promote 2026-11-07
```

Manual and automatic promotion share the existing deployment and daily locks. The writer verifies the approved refit against the report and checks that the evaluator's champion is still current. It registers the candidate and previous champion by checksum, preserves `previous.joblib`, journals the transaction in `promotion-pending.json`, and atomically publishes `model-activation.json`. That instruction contains a unique activation ID, version, target SHA-256, and expected base SHA-256. A local filesystem lock serializes the manifest replacement with the worker's final publication check. The API mount stays read-only.

The worker checks the instruction, verifies the candidate checksum and embedded version, and warms the bundle while traffic continues on the current snapshot. Every request captures one snapshot for selection, inference, and audit. The existing `/health` fields report the actual default. `activation_id`, `activation_status`, and `activation_error` report acknowledgement or an operator-safe failure code. The script requires the exact ID and loaded digest before running the existing recommendation smoke check. Only then does it update the evaluator's `model.joblib` alias, write `promotion.json`, clear the pending journal, and record the automatic outcome as before. The smoke check exercises the public route and database. If experiments are enabled it can hit a pinned arm, so the digest check and candidate warmup also matter.

```bash
bash deployment/daily_model.sh rollback 2026-11-07
```

Failed loading or smoke verification requests rollback to the previous immutable artifact. Rollback has its own activation ID and must pass the same verification before the alias and outcome are finalized. If verification fails, the transaction stays pending and new daily evaluations wait for recovery. After interruption, run rollback for the pending date under the same locks. This works before or after the forward acknowledgement, including when a worker starts from the last verified alias and reconciles the manifest. Repeating the rollback uses the same instruction. A newer promotion blocks rollback of an older transaction. Keep the manifest, lock file, registrations, and journal together. The protocol assumes a trusted writer and a local filesystem with atomic rename and advisory locking.

Older API revisions lack the activation capability and the new script refuses to promote against them. Resolve a pending transaction from an older checkout using that revision's recovery instructions before upgrading. Daily **model** updates and rollback use the existing worker. The initial API image rollout and future code deployments can recreate the single API and interrupt service.

## Register and pin experiment models

```bash
bash deployment/daily_model.sh register-champion
bash deployment/daily_model.sh register 2026-11-07
```

These commands register artifacts under the same locks without opening an experiment or switching the default. The first registers the current champion, including a matching legacy `model.json` if available. The second registers an approved refit from the daily report. `models/<sha256>.joblib` holds immutable bytes. The version registration in `models/` binds one version label to one digest and retains training metadata or a source report. Keep the registrations and matching training snapshots for provenance.

Compose defaults to disabled experiments. For a future explicitly authorized experiment, register both versions before starting the worker with `EXPERIMENTS_ENABLED=true`, `EXPERIMENT_ID`, `CONTROL_MODEL_VERSION`, and `TREATMENT_MODEL_VERSION`. For licensing cost, set `COST_AWARE_TREATMENT_ENABLED=true` and use the same artifact in both arms to isolate reranking. The Treatment fingerprint is still derived from the pinned version and cost configuration, with weight `0.15` by default. Control uses its pinned model version as the assignment version. `experiment_models_ready` indicates whether configured pins loaded. A missing or mismatched pin uses the verified default without a fulfilled exposure. Pin changes require a new experiment configuration and a worker startup. Promotions leave the configured pins in place.

For a past recommendation, use `recommendation_serves.request_id` to find its actual `model_version` and route. Resolve that version through its immutable registration to the artifact digest and training metadata. The request log also includes its digest. For a daily model, trace the report's source and snapshot checksums to the corresponding retained run. For the original model, use the matching training sidecar. Historical serves with ambiguous labels or missing training records cannot be reconstructed from the registry alone.

## Capacity and verification

`ModelRegistry.snapshot()` captures the ready handles, default, pins, and acknowledgement. `refresh()` processes a single manifest. The worker's `watch()` applies it at startup and polls for changes.

The registry assumes the default Dockerfile configuration of one Gunicorn worker with four threads. It stages one candidate at a time and caps live handles at five, including snapshots held by in-flight requests. Budget for the default, independently pinned Control and Treatment, the previous default, and a staged candidate, with duplicates sharing a handle. Measure peak RSS and temporary deserialization and warmup allocations under request load and concurrent updater activity. The count limit does not guarantee that those bundles fit in memory. The registry never evicts a configured pin. Keep sufficient disk space for immutable artifacts and training evidence. Disk retention is an operator responsibility.

Run `python -m pytest src/tests/test_model_registry.py src/tests/test_experiment_api.py src/tests/test_daily_updates.py src/tests/test_promotion_wrapper.py` for offline coverage. The isolated Docker verification job exercises synthetic auto-promotion, retry, and rollback with continuous HTTP traffic. It writes `ci-model-traffic.json` with errors, latency, served versions, and container and worker identity. Before scheduling production updates, verify actual worker count, model memory and load time, filesystem permissions, disk space, and the public latency and availability probes. Confirm the rollout and activation contract with the deployment owner. PR CI does not access production.
