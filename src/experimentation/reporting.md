# Experiment Reporting

Reports analyze persisted user assignments, actual ledger charges and recorded
ratings. They do not run inference, activate experiments, or substitute offline
reranking diagnostics for an online A/B result. No production results are included
here. Model hosting is not required to test reporting.

## Sources and Attribution

| Model / field | Meaning |
| --- | --- |
| `Experiment.experiment_id`, `starts_at`, `ends_at`, `status` | Experiment and observation boundaries; policy/version fields describe configured intent |
| `ExperimentAssignment.assignment_id`, `experiment_id`, `user_id`, `variant`, `assigned_at` | Persistent randomized user cohort; unique experiment/user pair |
| `RecommendationServe.request_id`, `experiment_assignment_id`, `served_at`, `model_route`, `model_version` | Application-attested completed exposure and actual serving metadata |
| `StreamEvent.event_id`, `user_id`, `movie_id`, `event_type`, `event_time`, `stored_at`, `rating` | Source occurrence time versus ingestion time; ratings are **1–10** |
| `LicensingCost.licensing_cost_id`, `source_event_id`, `user_id`, `movie_id`, `charged_at`, `license_cost` | Actual nullable Decimal ledger amount, uniquely linked to a stream event |
| `ExperimentReport.report_id`, `window_start`, `window_end`, `status`, `results`, `analysis_version`, `generated_at` | Immutable report revision using existing JSONB persistence |

`database.interactions.experiment_data.load()` uses `session_scope()` and a
PostgreSQL repeatable-read, read-only transaction. It makes six bounded bulk reads
for a populated cohort, independent of user count. The default limit is 200,000
rows **per source query**; exceeding it fails explicitly instead of truncating.
Use smaller windows for larger experiments. No initialization or migration runs.
SQLite is supported for isolated tests.

All external timestamps must include a timezone and are normalized to UTC.
Windows are half-open `[start, end)`, clipped to known experiment boundaries.
Assignments at or after the effective end are excluded. Each user's observation
starts at `max(assigned_at, window_start, experiment.starts_at)` and ends at the
effective window end. A missing experiment start is disclosed, not invented.
Windows must end no later than snapshot time. `stored_at` bounds event visibility;
this is a present database snapshot, not a historical reconstruction of mutable
experiment metadata. Enrollment can produce unequal follow-up; duration summaries
are reported. Assignments do not store historical eligibility or configuration.

Events and serves are deduplicated by UUID, charges by `source_event_id`. The
course consumer derives event UUIDs from Kafka topic/partition/offset; replays
with different offsets can have different UUIDs. Identical rating payloads with
different UUIDs are flagged, not silently deleted. A charge must reference a watch
with matching user, movie and occurrence/charge time. Invalid links remain unknown.
Concurrent experiment assignments are flagged for possible interference.

An exposure requires a target assignment FK, matching user, nonempty output and
known route/version in the user's observation interval. This trusts the existing
serving contract, not a client acknowledgement. Null links are normal/unlinked
serves and can include fallback. A Treatment-assigned user stays in Treatment
regardless of fallback or lack of exposure. Repeated serves never multiply events
or charges. Actual model versions come from serve records; configured Treatment
policy versions are reported separately.

Watch/rating events have no recommendation request or assignment ID. Only
assignment-based post-assignment outcomes are defensible here: they include
organic watches. No direct recommendation-to-watch join or exposure-conditioned
causal estimate is produced. The existing monitoring helper
`rating_attributions.search()` uses a user/movie/time-window heuristic that can
match several model identities; it is not a stable source linkage and is not used
to split randomized outcomes across model groups.

## Outcomes and Missing Data

The primary estimand is the mean **actual licensing charge sum per assigned user**
within that user's observation interval, including users with no activity.
`licensing_costs.license_cost` is the only monetary outcome source.
`movies.license_cost` is reference ranking metadata and is never queried by reporting.
Monetary totals use Decimal and serialize as decimal strings; currency is not
encoded in the schema, so do not label amounts USD without an external contract.
LLM charges are excluded.

`stream_events._materialize_event()` charges the first watch and watches at least
24 hours after the previous recorded watch of that movie. These events often
represent playback minutes, not independent viewing sessions. A 24-hour lookback
classifies uncharged watches consistent with the inactivity grace rule separately
from unexplained missing charges. Ingestion order can affect the existing billing
materialization; observed out-of-order pairs and charges inside expected grace
are flagged. Reporting does not repair or recalculate the ledger.

The schema contains **no completeness watermark**. No ledger row does not prove
zero spending. By default:

- Known, nonnegative, source-linked charges are an observed-cost lower bound.
- NULL, malformed, negative and nonfinite amounts remain unknown.
- Zero observed known cost is distinct from confirmed zero actual cost.
- The primary all-user actual-cost effect/test is withheld if either arm is incomplete.
- Lower-bound ledger comparisons are explicitly exploratory. A difference of
  arm-specific lower bounds is **not** a bound on the true Treatment effect.

An operator may supply `CoverageEvidence(complete_through, reference)` only with
independently verified complete delivery **and correct ordered billing** through
that boundary. It is an external attestation, not an inferred watermark. The
reference must be a nonsecret evidence ID. Coverage must reach the observation
end and cannot exceed snapshot time. Unknown amounts, broken source links and
unexplained missing charges still block complete cost identification. With valid
coverage and no unresolved user anomalies, a no-charge user has confirmed zero.
No development command supplies production evidence automatically.

Satisfaction is the equal-weight mean of each **observed rater's** mean valid
post-assignment rating. Users with no ratings remain in participation denominators
but are never assigned a rating of zero. This is a conditional satisfaction
estimand: Treatment can change who watches/rates, so its difference alone does
not identify satisfaction changes for all randomized users. Rating participation,
watch participation and watch-event counts per assigned user are complementary
outcomes. Without completeness evidence these also describe observed ingestion.

## Inference and Status

`experimentation.statistics` contains pure functions:

- `welch(control, treatment)`: user-level mean difference, relative difference
  when Control is nonzero, two-sided Welch t-test and 95% Satterthwaite interval.
- `bootstrap_difference(control, treatment, seed=42, repetitions=2000)`:
  independent user resampling and percentile 95% interval, as a robustness check.
  Repetitions are bounded to 100–5,000. A degenerate empirical interval is flagged.
- `participation(control, treatment)`: user-level risk difference, Newcombe-Wilson
  95% interval and two-sided Fisher exact p-value.

Differences are Treatment minus Control. Raw observations are users, never serves,
watch events or individual ratings. Missing/nonfinite inputs are excluded and
counted. The primary test is not run on a selected complete-case cost subset.
Welch requires at least two users per arm and positive finite sampling variance;
undefined or insufficient tests return no p-value/interval. Binary inference
requires a user in each arm. Statistical validity still depends on independent
randomization, data quality and reasonable sampling behavior. Skew and small
samples can make asymptotic inference unreliable; bootstrap does not fix selection
or ingestion bias. Nonsignificance is not equivalence.

Cost is primary; satisfaction and engagement tests are secondary/exploratory.
Intervals/p-values are unadjusted. Preregister the analysis window, multiplicity
and stopping rules before using periodic reports for decisions; repeated looks
are not automatically valid sequential tests.

`reporting.generate_report(*, experiment_id, window_start, window_end,
persist=False, coverage=None, seed=42, repetitions=2000, as_of=None)` returns JSON
sections for metadata, window, population, exposures, costs, satisfaction,
engagement, tests, data quality, methodology and limitations. The analysis version
is `assignment-user-outcomes-v1`.

- `insufficient_data`: fewer than two assigned users in either arm, or no observed
  watch/rating/charge evidence. Empty cohorts contain no fabricated effects.
- `provisional`: other generated reports, including ongoing experiments,
  incomplete costs, unresolved diagnostics or undefined primary/rating tests.
- `final`: completed experiment with known elapsed end/start, complete all-user
  costs, no flagged integrity/replay/interference issues, and defined primary
  cost and conditional-rating Welch tests. This is operational completeness,
  not significance, a power guarantee or team approval.
- `failed`: reserved schema status; execution failures return an error and roll
  back persistence instead of writing a partial statistical report.

## Persistence and Retry Policy

Preview is read-only. Explicit persistence uses
`experiment_reports.save_revision(ExperimentReportCreate(...))`. A canonical
SHA-256 fingerprint covers source rows, experiment configuration, window, coverage,
analysis version, resampling parameters and status. Unchanged retries return the
original immutable report, including its original generation time. Changed data
or settings append a new revision; no historical rows are deleted.

A deterministic UUID5 from experiment/window/version/fingerprint and the existing
report primary key provide concurrent insert-on-conflict protection. No migration
or new uniqueness constraint is needed. This contract applies to `save_revision`,
not older callers using random-UUID `create()`. PostgreSQL persistence uses the
repository's normal READ COMMITTED transaction; retry the entire command after a
transient transaction failure. Read and write transactions are separate; reports
identify the read snapshot and fingerprint, not a claim that source data cannot
change later. Persisted source fingerprints contain no raw user IDs in output.

## CLI and Worker

Run from the repository root against an **explicitly authorized** database using
the existing database configuration. These examples do not execute a deployment:

```sh
# Default read-only preview; aware timestamps are mandatory.
python -m src.experimentation.report_cli --experiment-id <UUID> \
  --start 2026-11-19T00:00:00Z --end 2026-11-22T00:00:00Z --dry-run

# JSON export without persistence.
python -m src.experimentation.report_cli --experiment-id <UUID> \
  --start 2026-11-19T00:00:00Z --end 2026-11-22T00:00:00Z \
  --json --output /tmp/experiment-report.json

# Append an idempotent revision explicitly.
python -m src.experimentation.report_cli --experiment-id <UUID> \
  --start 2026-11-19T00:00:00Z --end 2026-11-22T00:00:00Z --persist

# Disabled by default: no DB access. A future scheduler may invoke one enabled run.
python -m src.experimentation.report_worker
python -m src.experimentation.report_worker --enabled --experiment-id <UUID> \
  --start 2026-11-19T00:00:00Z --end 2026-11-22T00:00:00Z --persist
```

Use `--seed` and `--repetitions` for reproducible bootstrap configuration. Only
when verified evidence exists, supply both `--coverage-through <aware timestamp>`
and `--coverage-reference <nonsecret evidence ID>`. Do not put credentials in any
argument or exported metadata. Errors print sanitized exception types, never DB
URLs or source payloads. Exit codes: 0 generated/disabled, 2 invalid inputs,
1 missing experiment, database, persistence or output failure. An insufficient
report is a successful diagnostic and exits 0. The worker creates no background
process and installs no schedule; explicit `--enabled` and `--persist` are both
required to write through that entry point.

## Tests and Synthetic Example

```sh
python -m pytest src/tests/test_experiment_statistics.py src/database/tests/test_experiment_reporting.py
# Disposable local PG cluster only; see database/README.md for binaries.
M3_TEST_POSTGRES_BIN_DIR=/path/to/pg/bin python -m pytest \
  src/database/tests/test_experiment_migration_postgres.py
```

The reusable `seed_reporting_database()` test fixture records actual **synthetic**
ledger charges of 4 and 2, then changes catalog reference prices to 999. Two users
per arm remain in the cohort, including one zero-watch user per arm. Multiple
serves do not multiply charges. A short projection of its default preview is:

```json
{
  "data_origin": "synthetic_test_fixture",
  "analysis_version": "assignment-user-outcomes-v1",
  "status": "provisional",
  "population": {"control": {"assigned_users": 2}, "treatment": {"assigned_users": 2}},
  "licensing_cost": {
    "control": {"total_known_recorded_cost": "4.0000000000", "actual_cost_per_assigned_user": null},
    "treatment": {"total_known_recorded_cost": "2.0000000000", "actual_cost_per_assigned_user": null}
  },
  "user_satisfaction": {"control": {"mean_user_rating": 7.0}, "treatment": {"mean_user_rating": 6.0}}
}
```

This is a **synthetic fixture projection, not a live experiment result**. Its
primary actual-cost effect remains unidentified without external coverage.
Statistical confidence on tiny fixtures is only software validation.

## Live Experiment Dependencies

Before reporting real outcomes, the team needs approved immutable policies/lambda,
working multi-version hosting from Alvajoy, authorized live enrollment, trustworthy
Kafka ingestion and ordered charge materialization, currency/billing semantics,
and an auditable completeness watermark. Agree on a fixed follow-up/window,
power/stopping/multiplicity plan and handling of concurrent experiments. Freeze
lambda before activation; reporting does not change its current 0.15 default.

Persisting a recommendation request ID plus assignment/policy provenance on watch,
rating and charge events would improve direct attribution. Also useful: durable
Kafka offsets/completeness checkpoints, stable logical event IDs across replays,
client delivery acknowledgement, eligibility/configuration history and treatment
fallback reasons. Current reporting explicitly discloses their absence. The course
write-up must link this implementation and use real authorized experiment data;
it cannot use these fixture values or offline diagnostics as experiment results.
