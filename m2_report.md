# Milestone 2: Model and Infrastructure Quality

**Team:** 1 
**Repository:** https://github.com/jwatson8/The-Neural-Network

### Automated Model Updates (0.5 page max)

Our [daily updater](https://github.com/jwatson8/The-Neural-Network/blob/main/src/model_updates/daily.py) runs at 2 a.m. Eastern and combines the original dataset with accumulated PostgreSQL interactions to retrain our SVD recommender. It first trains an evaluation candidate using data available before the previous day, then compares it with the current model on that day's held-out ratings. Promotion requires at least 100 evaluated users, a positive one-sided 95% bootstrap lower bound for NDCG@20 improvement, inference p95 below 600 ms, and passing drift checks. An eligible candidate is separately refitted with interactions through the end of that day; the comparison scores describe the evaluation candidate, not this final refit.

With automatic promotion enabled, a [scheduled GitHub workflow](https://github.com/jwatson8/The-Neural-Network/blob/main/.github/workflows/daily-model.yml) runs at 08:17 UTC and invokes [`deployment/daily_model.sh auto`](https://github.com/jwatson8/The-Neural-Network/blob/main/deployment/daily_model.sh) on the VM. The script records comparison evidence in `model_logs`, verifies artifact checksums, backs up the current model, replaces it, and restarts the API. It checks the loaded model and a recommendation response; failed verification triggers rollback. Missing data or failed quality gates retain the current model. Snapshots, hashes, reports, and deployment outcomes make runs traceable; shared locks prevent concurrent training and deployment. The same script can trigger this process manually.

### Offline Evaluation (1 page of text max, excluding figures)

#TODO 

### Online Evaluation (1 page of text max, excluding figures)

See [Online evaluation](planning/Milestone%202/online_evaluation.md) for the metrics, telemetry, operationalization, and implementation links.

### Data Quality (0.5 page max)

The data quality framework provides reusable boundary validators for Kafka stream events, User/Movie data mapped to the database schemas, and Recommendation API requests and responses. User/Movie validation uses verified field mappings aligned with the database schemas. Validators enforce required fields, data types, and numerical constraints. Optional internal/telemetry model scores must be finite and within inclusive `[-1, 1]`; out-of-range values are rejected. The public Recommendation API returns movie IDs only. Invalid inputs generate structured validation errors. For PostgreSQL event ingestion, a dedicated handoff delegates validation to the shared database schema, allowing events to be validated once before persistence.

For drift detection, reusable functions compare historical and recent rating and event-type distributions using **Total Variation Distance (TVD)** and measure changes in missing-value rates. Initial thresholds are `0.20` (TVD) and `0.10` (10 percentage points of missingness), with at least `100` samples per window. These configurable thresholds provide an initial sensitivity setting for detecting substantial distribution changes. A modular database reader retrieves all pages within nonoverlapping time windows for rating and event-type drift analysis. Missingness detection requires separately supplied pre-validation raw snapshots, including rejected records, rather than accepted database records alone.

Validation failures and drift results generate sanitized structured JSON logs, with persistence through a dedicated PostgreSQL `data_quality_logs` table. Records include timestamps, severity, error details, and optional correlation IDs. Storage failures fall back to Python logging without propagating sink exceptions to the caller; synchronous persistence remains subject to database latency and timeouts. The current test suite contains **169 main-suite test methods and 13 database test methods**. Automated tests use isolated fixtures and mocked dependencies to verify validation, drift detection, logging, and database interactions.


### Pipeline Implementation and Testing (1 page of text max, excluding figures)

#TODO 

---

### Continuous Integration (0.5 page max)

Our [GitHub Actions workflow](https://github.com/jwatson8/The-Neural-Network/blob/main/.github/workflows/integration.yml) runs on every push and pull request. On GitHub-hosted Ubuntu with Python 3.12, it installs the declared test dependencies, runs pytest with coverage, and publishes test results and coverage reports as the `test-reports` artifact. Unit tests disable network sockets and use fixtures or mocks for external systems, so they require no production credentials, Kafka access, user/movie API calls, or paid LLM requests.

After tests pass, a Docker verification job validates Compose configuration, builds the service image, trains on the checked-in dataset, and starts isolated containers. It checks API health and recommendation responses, confirms that an empty production-data window preserves the current model, and exercises automatic model promotion, repeat-run protection, audit logging, and rollback using explicitly synthetic comparison evidence. Container logs are saved as `docker-reports`. A separate, manually requested deployment job runs only for `main` after both checks pass; it uses the VM runner and SSH to deploy the tested commit. [Passing main-branch run](https://github.com/jwatson8/The-Neural-Network/actions/runs/42552189761) verifies the infrastructure checks; the synthetic checks establish deployment mechanics, not live model-quality improvement.

## Final Submission Checklist (remove before submission)

- [ ] Replace every `#TODO` with the team's verified content.
- [ ] Check the page limits for each section.
- [ ] Include the final team-contract statement or revised contract.
- [ ] Commit `m2_report.md` at the repository root; submit the latest commit URL to Canvas and schedule the mentor debrief.
