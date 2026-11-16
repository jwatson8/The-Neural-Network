# Milestone 2: Model and Infrastructure Quality

**Team:** 1 
**Repository:** https://github.com/jwatson8/The-Neural-Network

# Automated Model Updates (0.5 page max)

Our [daily updater](https://github.com/jwatson8/The-Neural-Network/blob/main/src/model_updates/daily.py) runs at 2 a.m. Eastern and combines the original dataset with accumulated PostgreSQL interactions to retrain our SVD recommender. It first trains an evaluation candidate using data available before the previous day, then compares it with the current model on that day's held-out ratings. Promotion requires at least 100 evaluated users, a positive one-sided 95% bootstrap lower bound for NDCG@20 improvement, inference p95 below 600 ms, and passing drift checks. An eligible candidate is separately refitted with interactions through the end of that day; the comparison scores describe the evaluation candidate, not this final refit.

With automatic promotion enabled, a [scheduled GitHub workflow](https://github.com/jwatson8/The-Neural-Network/blob/main/.github/workflows/daily-model.yml) runs at 08:17 UTC and invokes [`deployment/daily_model.sh auto`](https://github.com/jwatson8/The-Neural-Network/blob/main/deployment/daily_model.sh) on the VM. The script records comparison evidence in `model_logs`, verifies artifact checksums, backs up the current model, replaces it, and restarts the API. It checks the loaded model and a recommendation response; failed verification triggers rollback. Missing data or failed quality gates retain the current model. Snapshots, hashes, reports, and deployment outcomes make runs traceable; shared locks prevent concurrent training and deployment. The same script can trigger this process manually.

# Offline Evaluation (1 page of text max, excluding figures)

**Data and split.** We evaluate the current 32-factor model on the fixed course dataset using a global chronological holdout, rather than splitting each user's timeline independently. After validation and exact-record deduplication, we discard account-created events and choose the timestamp at the 80th percentile of interaction events. All interactions before **2025-01-31 08:43 UTC** train the model; all at or after it are held out. This gives **43,723 training and 10,931 test events**. Equal timestamps stay together. Settings are fixed without test-based tuning; no separate validation set is used.

**Metric.** Our primary metric is macro-averaged binary **NDCG@20**, supplemented by **Recall@20**. **Data gathered:** the latest held-out rating for each user/movie pair defines relevance, with ratings >=7 positive. **Operationalization:** exclude movies seen in training before ranking the common full catalog; omit users with no remaining positive labels. For each eligible user, sum `relevance/log2(rank+1)` over the top 20 and divide by the ideal discounted sum. Recall is the number of recommended relevant movies divided by all eligible relevant movies. Average each metric across users. A popularity baseline uses the same training data, catalog, exclusions, and labels.

| Cohort | Users | Model NDCG@20 | Model Recall@20 | Popularity NDCG@20 | Popularity Recall@20 |
|---|---:|---:|---:|---:|---:|
| All eligible | 199 | 0.1886 | 0.1753 | 0.1886 | 0.1753 |
| At least 10 trained distinct movies | 1 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| Below the SVD threshold | 198 | 0.1895 | 0.1762 | 0.1895 | 0.1762 |

NDCG rewards relevant movies near the top and matches our ranked-list output better than rating-error metrics. Global time separation prevents future interactions from other users entering training. However, the provided catalog/user metadata is a fixed full snapshot. Without timestamped pre-cutoff LLM profiles, cold users receive the implemented popularity fallback; this experiment does not measure LLM personalization. Nearly all eligible users are cold, explaining the tied baseline and preventing a strong conclusion about SVD quality. Unobserved preferences remain unknown, so sparse, exposure-biased labels limit interpretation.

[Implementation](https://github.com/jwatson8/The-Neural-Network/blob/main/src/model_updates/offline.py) reuses the [daily ranking metrics](https://github.com/jwatson8/The-Neural-Network/blob/main/src/model_updates/evaluation.py). [Complete measured results](planning/Milestone%202/offline_results.json) include data/source hashes and settings. Reproduce from the repository root with `python -m src.model_updates.offline`; no live API or database is required.

# Online Evaluation
## Tech Stack, Collection, and Monitoring
Our online evaluation and monitoring takes place through:
- [A Grafana dashboard](../../src/monitoring/grafana/),
- An layered [PostGreSQL database](../../src/database/) with mulitple tables for collecting production telemetry,
- A persistent [Monitoring layer](../../src/monitoring/) that computes summaries from those tables every 5/mins.

We captures and logs our application's inputs and outputs throughout the application-chain (e.g., we capture record kafka events through [stream_events](../../src/database/interactions/stream_events.py), we capture our reccomendation outputs through [reccomendation_serves](../../src/database/interactions/recommendation_serves.py)). These data streams and tables are utilitzed by [Monitoring Worker](../../src/monitoring/monitoring_worker.py) who traverses them, computes relevent calculations from recorded telemetry, and logs those findings in [monitoring_runs](../../src/database/interactions/monitoring_runs.py). The monitoring worker is run independently in its own [docker container as monitoring-worker](../../compose.yaml).

Those snapshots are used by Dashboards in Grafana. Dashboards visualize both the individual 5-minute datapoint, and also, from each datapoint, mark a sliding 24-hour window to calculate changing averages over time.

## Online Measurement

### Calculating License Costs
- *Metric*: Summed license costs in five-minute and 24-hour increments. 
- *Data*: Use collected recordings of user watch-events within a 5 minute window and reaching backwards 23 hours and 55 minutes.
- *Operationalization*: When the kafka event stream records a movie-watch experience, record the watch and check to see if the watch-event is part of the same viewing experience (we record the [laser time](../../src/database/interactions/user_movie_history.py) a user watched a movie, and suggest that users can continue the "same" viewing within 24-hours of their last viewing). If this is a new viewing, or outside the 24-hour grace window, check the license fee of the movie and record the license fee. Repeat this step over a definitive 5 minute window and record the license fee costs within that window. Then calculate the total-sum of that 5 minute window and 23 hours and 55 minutes before it. This gives you a rolling 24-hour window cost from that 5-min timestamp. See [sample here](../../src/monitoring/calculations/license_fees.py)

### Calculating LLM Fees (Estimate)
- *Metric*: Summed LLM fees in 5 minutes and 24 hour increments.
- *Data*: Database compilation that records each cold-start approach using an LLM (recording input tokens, output tokens, and provider, and model used) 
- *Operationalization*: At time of logging, match the input and output token costs for the model selected with the provider's fee-per-token menu. Divide the input tokens by a million (usually, although may vary based on model - this assumes we are using gpt 4.0 mini) and mulitply the rate by the per-million token rate (in this case 15 cents). Follow the same pricing for the output tokens (60 cents). Calculate the 5-minute increment cost of those LLM calls. Then, add them to a rolling window composed of the previous 23 hours and 55 minutes. See [sample here](../../src/monitoring/calculations/llm_costs.py).

### Calculating Online Accuracy
- *Metric*: Raw accuracy of model over an acceptable threhsold (rating 6 or higher) in a 5 minute and rolling 24 hour window.
- *Data*: Keep track of the [reccomendations your model serves](../../src/database/interactions/recommendation_serves.py) and the [watch and rating events of your users](../../src/database/interactions/stream_events.py).
- *Operationalization*: Over a 5 minute window, track the ratings of your users. Then, check to see if any of those users were serviced by any of your model routes (popularity_fallback, cold_start, or collaborative_filtering). For each, calculate the percent served over threshold / total attributable rating events from a reccomendation. Make sure not to count watch events not tied to reccomendation service. Sum the total numerators (composed of 5 min increments) and divide by the total denominators (composed of 5 min incremenets) e.g., (5+10+45) / (10 + 20 +50). This computes your rolling accuracy average. See [sample here](../../src/monitoring/calculations/live_model_accuracy.py).

## Avodiing Pitfalls
Data in production is always a challenge. We implemented a number of course concepts in our approach to online evaluation.

- *Summarization*: Our visualization of online accuracy, noted above, summarizes in two-distinct ways. First, it collapses individual events into 5-min buckets. Second, it summarizes those buckets accross a rolling 24-hour window. This removes some of the fine-granular texture that might be visible to us at a microscopic scale, but gives us a bigger global view of the application's accuracy (particularly in terms of route comparison).
- *Conformity*: Our robust [data quality pipeline](../../src/data_quality/) and [database ingestion schemas](../../src/database/schemas.py) ensures that raw data fits defined paramaters for our application, and works to keep malformed data out of pipelines.
- *Wait and See*: Our approach to measuring accuracy prioritizes definite results over speculative indicators. After making a reccomendation, we don't evaluate the accuracy of reccomendations until after a user rates that movie from the list. One cost with this approach is we don't evaluate immediete user's preference for our reccomendation (we don't count or calculate the percent of total reccomendations that are followed through currently, don't measure the immediete impact of our algorithm on user behavior, and don't account for users who start a reccomendation but never rate it). While this gives us a measure of accuracy in use, it trades-off with a more holistic picture of model accuracy and user satisfaction.

# Data Quality

The data quality framework provides reusable boundary validators for Kafka stream events, User/Movie data mapped to the database schemas, and Recommendation API requests and responses. User/Movie validation uses verified field mappings aligned with the database schemas. Validators enforce required fields, data types, and numerical constraints. Optional internal/telemetry model scores must be finite and within inclusive `[-1, 1]`; out-of-range values are rejected. The public Recommendation API returns movie IDs only. Invalid inputs generate structured validation errors. For PostgreSQL event ingestion, a dedicated handoff delegates validation to the shared database schema, allowing events to be validated once before persistence.

For drift detection, reusable functions compare historical and recent rating and event-type distributions using **Total Variation Distance (TVD)** and measure changes in missing-value rates. Initial thresholds are `0.20` (TVD) and `0.10` (10 percentage points of missingness), with at least `100` samples per window. These configurable thresholds provide an initial sensitivity setting for detecting substantial distribution changes. A modular database reader retrieves all pages within nonoverlapping time windows for rating and event-type drift analysis. Missingness detection requires separately supplied pre-validation raw snapshots, including rejected records, rather than accepted database records alone.

Validation failures and drift results generate sanitized structured JSON logs, with persistence through a dedicated PostgreSQL `data_quality_logs` table. Records include timestamps, severity, error details, and optional correlation IDs. Storage failures fall back to Python logging without propagating sink exceptions to the caller; synchronous persistence remains subject to database latency and timeouts. The current test results and coverage are linked in the Pipeline Implementation and Testing section. Automated tests use isolated fixtures and mocked dependencies to verify validation, drift detection, logging, and database interactions.


# Pipeline Implementation and Testing

Our pipeline separates [Kafka ingestion](https://github.com/jwatson8/The-Neural-Network/blob/main/src/service/course_stream.py), [boundary validation](https://github.com/jwatson8/The-Neural-Network/tree/main/src/data_quality), [PostgreSQL storage](https://github.com/jwatson8/The-Neural-Network/tree/main/src/database), [SVD training](https://github.com/jwatson8/The-Neural-Network/blob/main/src/model/recommender.py), [evaluation and model updates](https://github.com/jwatson8/The-Neural-Network/tree/main/src/model_updates), and [Flask serving/background profiling](https://github.com/jwatson8/The-Neural-Network/tree/main/src/service). Kafka events and API metadata are validated before persistence; interaction history feeds training, while the LLM worker asynchronously creates cold-start profiles. The serving process loads a serialized model and selects SVD, LLM content, or popularity according to trained history and profile availability. Independent monitoring processes aggregate recorded telemetry. Deployment commands and configuration are versioned with the code.

The [pytest suite](https://github.com/jwatson8/The-Neural-Network/blob/main/pytest.ini) covers parsing and malformed inputs, database constraints and transactions, pagination, cold-start job leases/retries, response formatting, profile generation through mocks, the ten-distinct-movie routing boundary, temporal splits, ranking metrics, drift gates, model checksums, and promotion/rollback behavior. Monitoring tests exercise attribution, costs, missing prices, bucket boundaries, and repeat processing. External calls use fixtures/mocks and network sockets are disabled in unit tests. Database interaction tests use temporary SQLite databases; Docker CI adds isolated PostgreSQL, model training, actual HTTP smoke checks, and synthetic promotion/rollback verification.

The local run completed with **273 tests passing, 6 skipped, and 434 subtests passing**, with **84% statement coverage**; the [coverage report](planning/Milestone%202/test_coverage.md) shows coverage by module rather than implying complete assurance. [The GitHub Linux test job](https://github.com/jwatson8/The-Neural-Network/actions/runs/42559448018/job/128972415409) passed all **279 tests** with no skips; Docker integration is checked separately in the same workflow. These checks target the interfaces and failure paths most likely to corrupt data or interrupt serving, without attempting to reimplement or prove scikit-learn's SVD algorithm. Mocked network tests and isolated containers do not establish real-course connectivity, production data completeness, sustained load, or live model improvement; those require deployment and telemetry evidence.

---

# Continuous Integration

Our [GitHub Actions workflow](https://github.com/jwatson8/The-Neural-Network/blob/main/.github/workflows/integration.yml) runs on every push and pull request. On GitHub-hosted Ubuntu with Python 3.12, it installs the declared test dependencies, runs pytest with coverage, and publishes test results and coverage reports as the `test-reports` artifact. Unit tests disable network sockets and use fixtures or mocks for external systems, so they require no production credentials, Kafka access, user/movie API calls, or paid LLM requests.

After tests pass, a Docker verification job validates Compose configuration, builds the service image, trains on the checked-in dataset, and starts isolated containers. It checks API health and recommendation responses, confirms that an empty production-data window preserves the current model, and exercises automatic model promotion, repeat-run protection, audit logging, and rollback using explicitly synthetic comparison evidence. Container logs are saved as `docker-reports`. A separate, manually requested deployment job runs only for `main` after both checks pass; it uses the VM runner and SSH to deploy the tested commit. [Passing main-branch run](https://github.com/jwatson8/The-Neural-Network/actions/runs/42552189761) verifies the infrastructure checks; the synthetic checks establish deployment mechanics, not live model-quality improvement.

# Team Contract Update

No update to team contract. Team-management responsibilities remain unchanged from Milestone 1.
