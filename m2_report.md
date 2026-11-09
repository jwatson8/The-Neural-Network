# Milestone 2: Model and Infrastructure Quality

**Team:** 1 
**Repository:** https://github.com/jwatson8/The-Neural-Network

### Automated Model Updates (0.5 page max)

Our [daily updater](https://github.com/jwatson8/The-Neural-Network/blob/main/src/model_updates/daily.py) runs at 2 a.m. Eastern and combines the original dataset with accumulated PostgreSQL interactions to retrain our SVD recommender. It first trains an evaluation candidate using data available before the previous day, then compares it with the current model on that day's held-out ratings. Promotion requires at least 100 evaluated users, a positive one-sided 95% bootstrap lower bound for NDCG@20 improvement, inference p95 below 600 ms, and passing drift checks. An eligible candidate is separately refitted with interactions through the end of that day; the comparison scores describe the evaluation candidate, not this final refit.

With automatic promotion enabled, a [scheduled GitHub workflow](https://github.com/jwatson8/The-Neural-Network/blob/main/.github/workflows/daily-model.yml) runs at 08:17 UTC and invokes [`deployment/daily_model.sh auto`](https://github.com/jwatson8/The-Neural-Network/blob/main/deployment/daily_model.sh) on the VM. The script records comparison evidence in `model_logs`, verifies artifact checksums, backs up the current model, replaces it, and restarts the API. It checks the loaded model and a recommendation response; failed verification triggers rollback. Missing data or failed quality gates retain the current model. Snapshots, hashes, reports, and deployment outcomes make runs traceable; shared locks prevent concurrent training and deployment. The same script can trigger this process manually.

### Offline Evaluation (1 page of text max, excluding figures)

**Data and split.** We evaluate the current 32-factor model on the fixed course dataset using a global chronological holdout, rather than splitting each user's timeline independently. After validation and exact-record deduplication, we discard account-created events and choose the timestamp at the 80th percentile of interaction events. All interactions before **2025-01-31 08:43 UTC** train the model; all at or after it are held out. This gives **43,723 training and 10,931 test events**. Equal timestamps stay together. Settings are fixed without test-based tuning; no separate validation set is used.

**Metric.** Our primary metric is macro-averaged binary **NDCG@20**, supplemented by **Recall@20**. **Data gathered:** the latest held-out rating for each user/movie pair defines relevance, with ratings >=7 positive. **Operationalization:** exclude movies seen in training before ranking the common full catalog; omit users with no remaining positive labels. For each eligible user, sum `relevance/log2(rank+1)` over the top 20 and divide by the ideal discounted sum. Recall is the number of recommended relevant movies divided by all eligible relevant movies. Average each metric across users. A popularity baseline uses the same training data, catalog, exclusions, and labels.

| Cohort | Users | Model NDCG@20 | Model Recall@20 | Popularity NDCG@20 | Popularity Recall@20 |
|---|---:|---:|---:|---:|---:|
| All eligible | 199 | 0.1886 | 0.1753 | 0.1886 | 0.1753 |
| At least 10 trained distinct movies | 1 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| Below the SVD threshold | 198 | 0.1895 | 0.1762 | 0.1895 | 0.1762 |

NDCG rewards relevant movies near the top and matches our ranked-list output better than rating-error metrics. Global time separation prevents future interactions from other users entering training. However, the provided catalog/user metadata is a fixed full snapshot. Without timestamped pre-cutoff LLM profiles, cold users receive the implemented popularity fallback; this experiment does not measure LLM personalization. Nearly all eligible users are cold, explaining the tied baseline and preventing a strong conclusion about SVD quality. Unobserved preferences remain unknown, so sparse, exposure-biased labels limit interpretation.

[Implementation](https://github.com/jwatson8/The-Neural-Network/blob/main/src/model_updates/offline.py) reuses the [daily ranking metrics](https://github.com/jwatson8/The-Neural-Network/blob/main/src/model_updates/evaluation.py). [Complete measured results](planning/Milestone%202/offline_results.json) include data/source hashes and settings. Reproduce from the repository root with `python -m src.model_updates.offline`; no live API or database is required.

### Online Evaluation (1 page of text max, excluding figures)

See [Online evaluation](planning/Milestone%202/online_evaluation.md) for the metrics, telemetry, operationalization, and implementation links.

### Data Quality (0.5 page max)

The data quality framework provides reusable boundary validators for Kafka stream events, User/Movie data mapped to the database schemas, and Recommendation API requests and responses. User/Movie validation uses verified field mappings aligned with the database schemas. Validators enforce required fields, data types, and numerical constraints. Optional internal/telemetry model scores must be finite and within inclusive `[-1, 1]`; out-of-range values are rejected. The public Recommendation API returns movie IDs only. Invalid inputs generate structured validation errors. For PostgreSQL event ingestion, a dedicated handoff delegates validation to the shared database schema, allowing events to be validated once before persistence.

For drift detection, reusable functions compare historical and recent rating and event-type distributions using **Total Variation Distance (TVD)** and measure changes in missing-value rates. Initial thresholds are `0.20` (TVD) and `0.10` (10 percentage points of missingness), with at least `100` samples per window. These configurable thresholds provide an initial sensitivity setting for detecting substantial distribution changes. A modular database reader retrieves all pages within nonoverlapping time windows for rating and event-type drift analysis. Missingness detection requires separately supplied pre-validation raw snapshots, including rejected records, rather than accepted database records alone.

Validation failures and drift results generate sanitized structured JSON logs, with persistence through a dedicated PostgreSQL `data_quality_logs` table. Records include timestamps, severity, error details, and optional correlation IDs. Storage failures fall back to Python logging without propagating sink exceptions to the caller; synchronous persistence remains subject to database latency and timeouts. The current test results and coverage are linked in the Pipeline Implementation and Testing section. Automated tests use isolated fixtures and mocked dependencies to verify validation, drift detection, logging, and database interactions.


### Pipeline Implementation and Testing (1 page of text max, excluding figures)

Our pipeline separates [Kafka ingestion](https://github.com/jwatson8/The-Neural-Network/blob/main/src/service/course_stream.py), [boundary validation](https://github.com/jwatson8/The-Neural-Network/tree/main/src/data_quality), [PostgreSQL storage](https://github.com/jwatson8/The-Neural-Network/tree/main/src/database), [SVD training](https://github.com/jwatson8/The-Neural-Network/blob/main/src/model/recommender.py), [evaluation and model updates](https://github.com/jwatson8/The-Neural-Network/tree/main/src/model_updates), and [Flask serving/background profiling](https://github.com/jwatson8/The-Neural-Network/tree/main/src/service). Kafka events and API metadata are validated before persistence; interaction history feeds training, while the LLM worker asynchronously creates cold-start profiles. The serving process loads a serialized model and selects SVD, LLM content, or popularity according to trained history and profile availability. Independent monitoring processes aggregate recorded telemetry. Deployment commands and configuration are versioned with the code.

The [pytest suite](https://github.com/jwatson8/The-Neural-Network/blob/main/pytest.ini) covers parsing and malformed inputs, database constraints and transactions, pagination, cold-start job leases/retries, response formatting, profile generation through mocks, the ten-distinct-movie routing boundary, temporal splits, ranking metrics, drift gates, model checksums, and promotion/rollback behavior. Monitoring tests exercise attribution, costs, missing prices, bucket boundaries, and repeat processing. External calls use fixtures/mocks and network sockets are disabled in unit tests. Database interaction tests use temporary SQLite databases; Docker CI adds isolated PostgreSQL, model training, actual HTTP smoke checks, and synthetic promotion/rollback verification.

The local run completed with **273 tests passing, 6 skipped, and 434 subtests passing**, with **84% statement coverage**; the [coverage report](planning/Milestone%202/test_coverage.md) shows coverage by module rather than implying complete assurance. [The GitHub Linux test job](https://github.com/jwatson8/The-Neural-Network/actions/runs/42559448018/job/128972415409) passed all **279 tests** with no skips; Docker integration is checked separately in the same workflow. These checks target the interfaces and failure paths most likely to corrupt data or interrupt serving, without attempting to reimplement or prove scikit-learn's SVD algorithm. Mocked network tests and isolated containers do not establish real-course connectivity, production data completeness, sustained load, or live model improvement; those require deployment and telemetry evidence.

---

### Continuous Integration (0.5 page max)

Our [GitHub Actions workflow](https://github.com/jwatson8/The-Neural-Network/blob/main/.github/workflows/integration.yml) runs on every push and pull request. On GitHub-hosted Ubuntu with Python 3.12, it installs the declared test dependencies, runs pytest with coverage, and publishes test results and coverage reports as the `test-reports` artifact. Unit tests disable network sockets and use fixtures or mocks for external systems, so they require no production credentials, Kafka access, user/movie API calls, or paid LLM requests.

After tests pass, a Docker verification job validates Compose configuration, builds the service image, trains on the checked-in dataset, and starts isolated containers. It checks API health and recommendation responses, confirms that an empty production-data window preserves the current model, and exercises automatic model promotion, repeat-run protection, audit logging, and rollback using explicitly synthetic comparison evidence. Container logs are saved as `docker-reports`. A separate, manually requested deployment job runs only for `main` after both checks pass; it uses the VM runner and SSH to deploy the tested commit. [Passing main-branch run](https://github.com/jwatson8/The-Neural-Network/actions/runs/42552189761) verifies the infrastructure checks; the synthetic checks establish deployment mechanics, not live model-quality improvement.

### Team Contract Update

No update to team contract. Team-management responsibilities remain unchanged from Milestone 1.
