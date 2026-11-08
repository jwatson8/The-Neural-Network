# Daily model updates

The daily job **prepares and evaluates models; it does not replace production automatically**. A teammate reviews the report and explicitly promotes an eligible candidate. This is a conservative first version of automated updates.

## After merging and deploying

Run the existing GitHub deployment workflow on `main`. Compose now starts `daily-model-updater` alongside the existing services. It uses the same image as the API, with a 1-CPU/2-GB limit and one numerical-computing thread. There is no Docker socket inside this container. The deployment sets its UID/GID to the deployment account so it can write `artifacts/`.

The scheduler checks once per minute and processes the **previous Eastern calendar day after 02:00 America/New_York**. After a restart it catches up on yesterday only, not every missed day. On the spring DST transition it runs after the missing 02:00 hour (03:00); calendar-day windows still handle 23/25-hour days correctly. Setting `DAILY_MODEL_UPDATES_ENABLED=false` in the VM `.env` and recreating the updater disables scheduling.

```bash
cd /home/the-neural-network
docker compose ps daily-model-updater
docker compose logs --tail=50 daily-model-updater
```

**Data prerequisite:** the PostgreSQL `stream_events`, `users`, and `movies` tables must contain production data. At implementation time `main`'s Kafka consumer only stores account/recommendation evidence in SQLite; it does not yet feed PostgreSQL watch/rating rows. Coordinate with the ingestion owner. Deploying this PR alone does not fix that gap. The daily job reports `insufficient_data` when there are no fresh events; it never claims it trained on new data in that case.

The online report also needs PostgreSQL `recommendation_serves` and `llm_usage`. The evaluator uses only cold-start profiles with `updated_at` before the test day from PostgreSQL `cold_start_profiles`. Missing historical profiles use popularity fallback in offline evaluation; it does not regenerate profiles retroactively or query live APIs. Ensure the ingestion/telemetry work populates these tables before treating the daily report as complete production evidence. Existing SQLite profile serving remains unchanged.

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

## Approve and promote

Review the report, confirm ingestion/telemetry coverage, and choose a time for a short API restart:

```bash
bash deployment/daily_model.sh promote 2026-11-07
```

The command refuses a failed gate, a modified candidate, or a champion that changed since evaluation. It saves a backup, atomically replaces `artifacts/model.joblib`, restarts the API and verifies the loaded artifact checksum via `/health` plus a recommendation response. Failed verification restores the backup and checks the restored API; recovery failure exits with an error. Only the API restarts, not the database or ingestion services. The artifact's embedded model version is used in API logs/health instead of the old `.env` model label. This command never deletes database volumes or overwrites `.env`.

```bash
# Explicitly revert the most recent applicable promoted artifact, or recover a pending attempt:
bash deployment/daily_model.sh rollback 2026-11-07
```

Code deployment, scheduler training and manual promotion use shared file locks. If busy, retry after the current operation finishes. A process interruption can leave `artifacts/promotion-pending.json`; use the rollback command before another promotion. After success, `promotion.json` records the decision; `previous.joblib` is retained.

## Tests and limits

`python -m pytest` includes synthetic temporal split, late-arrival, DST, metric, data-read, rejection, artifact integrity and rollback tests. CI also invokes the job against isolated PostgreSQL and confirms an empty production window leaves the model unchanged. GitHub does not run the real daily job against production.

No automatic promotion, live A/B experiment, database ingestion rewrite, LLM call, API metadata fetch, continuous monitoring, or automatic snapshot deletion is introduced here. These are separate tasks. This PR provides the daily training/evaluation structure and a verified promotion mechanism; actual improvement and production update evidence must come from your VM data.
