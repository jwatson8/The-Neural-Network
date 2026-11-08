# Milestone 2: Model and Infrastructure Quality

**Team:** 1 
**Repository:** https://github.com/jwatson8/The-Neural-Network

### Automated Model Updates (0.5 page max)

#TODO 

### Offline Evaluation (1 page of text max, excluding figures)

#TODO 

### Online Evaluation (1 page of text max, excluding figures)

#TODO 

### Data Quality (0.5 page max)

The data quality framework provides reusable boundary validators for Kafka stream events, User/Movie data mapped to the database schemas, and Recommendation API requests and responses. User/Movie validation uses verified field mappings aligned with the database schemas. Validators enforce required fields, data types, and numerical constraints. Optional internal/telemetry model scores must be finite and within inclusive `[-1, 1]`; out-of-range values are rejected. The public Recommendation API returns movie IDs only. Invalid inputs generate structured validation errors. For PostgreSQL event ingestion, a dedicated handoff delegates validation to the shared database schema, allowing events to be validated once before persistence.

For drift detection, reusable functions compare historical and recent rating and event-type distributions using **Total Variation Distance (TVD)** and measure changes in missing-value rates. Initial thresholds are `0.20` (TVD) and `0.10` (10 percentage points of missingness), with at least `100` samples per window. These configurable thresholds provide an initial sensitivity setting for detecting substantial distribution changes. A modular database reader retrieves all pages within nonoverlapping time windows for rating and event-type drift analysis. Missingness detection requires separately supplied pre-validation raw snapshots, including rejected records, rather than accepted database records alone.

Validation failures and drift results generate sanitized structured JSON logs, with persistence through a dedicated PostgreSQL `data_quality_logs` table. Records include timestamps, severity, error details, and optional correlation IDs. Storage failures fall back to Python logging without propagating sink exceptions to the caller; synchronous persistence remains subject to database latency and timeouts. The current test suite contains **169 main-suite test methods and 13 database test methods**. Automated tests use isolated fixtures and mocked dependencies to verify validation, drift detection, logging, and database interactions.


### Pipeline Implementation and Testing (1 page of text max, excluding figures)

#TODO 

---

## Final Submission Checklist (remove before submission)

- [ ] Replace every `#TODO` with the team's verified content.
- [ ] Check the page limits for each section.
- [ ] Check all implementation links relative to the repository root.
- [ ] Add actual offline/online accuracy and cost results, not just plans.
- [ ] Verify the live integration and deployment claims in Section 4.
- [ ] Link a passing GitHub Actions run and a coverage report.
- [ ] Include the final team-contract statement or revised contract.
- [ ] Commit `m2_report.md` at the repository root; submit the latest commit URL to Canvas and schedule the mentor debrief.
