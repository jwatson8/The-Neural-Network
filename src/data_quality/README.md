# Response boundary validation

`validate_user_response(payload)` returns `(UserCreate, [])` on success.
`validate_movie_response(payload)` returns `(MovieCreate, [])` on success.
Failures return `(None, errors)` with `loc`, `type`, and `msg` entries, excluding
raw input and exception context. Inputs are decoded JSON objects, not HTTP
responses or JSON text. Neither function performs I/O or modifies the input.

## Verified scope

- `src/service/profile_worker.py:fetch_descriptions` accepts a user object or a
  `user` wrapper. Its default preference fields are `self_description_likes` and
  `self_description_dislikes`; deployments may configure different names.
- `deployment/README.md`, section 4, explicitly requires confirming the live
  User API URL and response field names. It does not establish an identifier or
  demographic response contract.
- `src/database/schemas.py:UserCreate` and `MovieCreate` establish persistence
  requirements, not external API contracts. `src/database/roll_up.py` maps the
  recorded CSV snapshot into those names; it is not a live API adapter.
- No Movie API response definition or response fixture was found.

The implemented subset therefore accepts database field names only, plus the
verified User wrapper. It does not guess `id` aliases, integer ID conversions,
genre list encodings, Movie wrappers, or configured preference aliases. Callers
must map those explicitly once the real contracts are available. A preference-only
User response requires the caller to supply its verified request identifier as
`user_id` before validation; this validator does not invent a missing ID.

## Boundary policy

Identifiers are trimmed and must remain nonempty strings. Other values follow
Nathan's schemas, including numeric string coercion, nullable optional fields,
nonnegative age/runtime/vote count/license cost, and rejection of extra fields.
Additionally, numeric booleans and non-finite numbers are rejected. No unverified
age maximum, popularity bound, vote-average range, or gender enumeration is added.
Unknown wrapper metadata is rejected rather than silently discarded. Error
locations for wrapped users include the `user` prefix.
Movie titles must contain at least one non-whitespace character. Nonblank titles
retain their original whitespace; the validator does not rewrite display text.

## User integration mapping

`fetch_descriptions` in `src/service/profile_worker.py` is a preference-only
consumer, not a `UserCreate` adapter. It requires at least one configured
preference key, does not require a response ID, unwraps `user` only when its value
is a dictionary, and ignores other fields. It reads `USER_LIKES_FIELD` and
`USER_DISLIKES_FIELD` (defaulting to the canonical description names). It applies
`value or ""` before checking for strings, so nulls and even falsy non-string
values become empty strings. These behaviors are not the persistence contract.

Before calling `validate_user_response()`, the integration owner must:

1. Decode the JSON and establish the actual response envelope. For the supported
   `user` dictionary wrapper, map its contents; alternatively preserve exactly
   `{"user": mapped_object}`. Do not pass wrapper metadata through as user fields.
   A non-dictionary `user` is rejected by this validator, even though the worker
   may fall back to top-level preference fields in that situation.
2. Resolve the configured likes/dislikes keys and map their values to
   `self_description_likes` / `self_description_dislikes`. Remove renamed source
   keys from the mapped object. Reject ambiguous conflicting source/canonical
   values rather than silently overwriting them. The validator does not read
   environment variables. If preserving worker lookup semantics, check that at
   least one configured key exists before mapping: `UserCreate` itself permits
   both descriptions to be absent.
3. Supply `user_id` from the verified lookup request context if the response has
   no ID. This is an existing identity, never a generated ID or a preference
   value. If a response ID exists, verify it matches that context under the
   confirmed API ID representation; reject mismatches. Do not silently overwrite
   a null or malformed response ID with the request ID. String ID conversion
   requires a confirmed API contract; the validator accepts only nonblank strings.
4. Pass only fields with a confirmed mapping to `UserCreate`. Handle unrelated
   API metadata explicitly outside this validator; passing unknown fields causes
   structured errors. Do not infer demographic values or add placeholder IDs.
5. Preserve missing optional preferences as absent (or null), explicit nulls as
   null, and empty strings as empty strings. Reject malformed preference types;
   do not copy the worker's broad falsy-value coercion into persistence mapping.
   If the downstream preference consumer needs empty strings, convert validated
   `None` values to `""` at that consumer boundary.

For example, a verified request for `test-user` with configured keys `likes` and
`dislikes` and response `{"user": {"likes": "fiction"}}` maps to
`{"user_id": "test-user", "self_description_likes": "fiction"}`. Passing the
original preference-only response directly fails for missing `user_id`; adding
the ID without renaming `likes` still fails for an unexpected field. The worker's
returned `(likes, dislikes)` tuple is not a valid input to this validator.

Remaining mismatches are intentional: worker responses can omit identity and
contain extra fields; persistence requires identity and rejects extra fields.
Worker aliases and preference-presence checks require an integration adapter.
The live ID field/representation, possible metadata and alias collisions still
need contract confirmation. No service integration is performed here.

Integration owners still need to confirm live field names, required response
fields, identifier types, null/empty conventions, numeric ranges, and envelopes;
then connect these validators to fetching/persistence and error handling. These
functions do not replace the existing preference-only worker lookup.

Run from the repository root with the service dependencies installed:

```sh
python -m unittest discover -s src/tests -p 'test_data_quality*.py' -v
python -m unittest discover -s src/tests -v
```

## Recommendation API

Verified sources:

- `src/service/api.py:recommendation`: `GET /recommend/<string:user_id>`;
  the route always supplies `k=20` internally and returns `text/plain` with
  `','.join(movie_ids)`. It reads no query parameters or request JSON.
- `src/service/model_adapter.py:recommend` and
  `src/service/cold_start.py:recommend_cold_start`: distinct movie IDs in ranked
  order, with non-finite internal scores filtered out; internal `k` is 1–20.
- `src/tests/test_service.py`, `src/tests/test_model_integration.py` and
  `deployment/smoke_test.py`: comma-separated IDs, ranking preserved, no CR/LF;
  the smoke test requires 1–20 unique IDs from the catalog, excluding seen movies.
- `src/database/schemas.py:RecommendationItem`: persistence item with `movie_id`
  and optional `model_score`. `RecommendationServeCreate` is database telemetry
  requiring timestamps, model metadata and timings, **not** the public response.

### Supported inputs and outputs

`validate_recommendation_request({"user_id": user_id}, query_params=None)`
returns a new `{"user_id": user_id}` dictionary or `(None, errors)`. The ID must
be a nonblank string without `/`, matching a single decoded Flask path segment.
Its exact spelling is preserved, including surrounding whitespace on nonblank
IDs, because changing it could select a different user. No numeric-only pattern,
existence requirement, or unverified public ID length limit is imposed.

Only `user_id` is a supported route value. Supplying any additional request key
or query parameter (including `k`, `limit`, numeric NaN or Infinity) is rejected.
This is an explicit boundary policy: the current Flask route ignores query
parameters. The API owner must decide how to surface those errors when wiring it
in; this implementation does not change current Flask behavior.

`validate_recommendation_response(body)` accepts the **decoded text body of a
successful public response**, such as `"movie-z,movie-a"`. It returns an ordered
`list[RecommendationItem]` or `(None, errors)`. It rejects empty bodies, more than
20 entries, empty/whitespace-only IDs, CR/LF, and exact duplicate IDs. Item IDs
reuse Nathan's 1–255 character schema. No trimming, sorting or deduplication
changes the response. Fewer than 20 IDs are allowed; exactly 20 is not required.
There are no required JSON fields: JSON objects, item lists, Flask Response
objects, bytes and numeric payloads are rejected. Errors on entries use locations
such as `("recommendations", 2, "movie_id")`.

`validate_recommendation_item({"movie_id": "movie-a", "model_score": -0.5})`
is a separate **internal/persistence item** helper, not a public response format.
It reuses Nathan's `RecommendationItem`, rejects extra fields and numeric boolean
scores, and requires present non-null scores to be finite and within inclusive
`[-1, 1]`, per Nathan's PR #41 review. Both endpoints are valid; values outside
the interval are rejected, never clamped. Optional scores can be missing or null.
Existing numeric string parsing is preserved, and input dictionaries are not
mutated. The same constraints apply to direct `recommendation_serves.create()`
callers through the shared schema.

This is a stricter telemetry contract than the current model mathematics:
`service/model_adapter.py` computes SVD dot products without a `[-1, 1]` guarantee;
`service/cold_start.py` combines similarities and normalized popularity with a
theoretical upper bound of 1.02. Mapping those raw scores into `model_score` can
now reject an entire serve audit, including otherwise valid recommendation items.
The offline `model/recommender.py` returns a `score` field and is unaffected until
mapped to this schema. The current Flask path returns/stores IDs and route/hash
evidence without numeric scores, so its ranking behavior remains unchanged.
Popularity fallback scores from the repository-built model remain within [0, 1].
No scoring algorithms, normalization or clamping are introduced by this change.
The existing M0 CLI in `src/model/recommender.py` emits `score`, along with title
and method data. It is not the HTTP contract or canonical persistence item: an
integration owner must explicitly map `score` to `model_score` and select fields
before using this helper. Neither JSON representation is accepted by the public
response validator. No scores are fabricated for the public ID-only response.

### Integration example

Within the Flask route, before inference:

```python
request_value, errors = validate_recommendation_request(
    {"user_id": user_id}, query_params=request.args
)
# API owner handles errors before using request_value["user_id"].
```

Before emitting a successful body:

```python
body = ",".join(movie_ids)
items, errors = validate_recommendation_response(body)
# API owner handles errors, then emits body as text/plain in the original order.
```

The caller must ensure the inference result contains string IDs before joining;
this validator does not validate HTTP status, content type, or raw model tensors.
For HTTP client use, first confirm status 200 and `text/plain`, then pass decoded
text. A 503 `Model unavailable` message is not a recommendation response.
Catalog membership, exclusion of seen movies and association with the requested
user need caller/model context and are not checked here. Opaque text IDs cannot
be distinguished from arbitrary error text or JSON-looking text without catalog
context. String tokens such as `NaN` are opaque IDs, not numeric scores; actual
numeric NaN/Infinity payloads and non-finite scored items are rejected.

Known uncertainty: inference can produce zero eligible candidates, while the
HTTP smoke test requires at least one. This validator follows the smoke contract
and rejects an empty success body. The API owner must define its failure response
for that case. The reused 255-character movie ID persistence limit is stricter
than the route's bare string joining; live catalogs must respect that limit.
Any future public score field, query parameter or JSON envelope requires an
explicit API contract change. HTTP error handling and telemetry construction
remain with the service owner; no service files are changed here.

## Data drift detection

`src/data_quality/drift.py` contains pure functions with no database, Kafka,
network, wall-clock or scheduling behavior. All accept baseline and recent
iterables of mappings or verified `StreamEventCreate` objects. They consume
generators once and leave records unchanged. Database `StreamEvent` rows should
be converted with `row.model_dump()` by the caller.

| Function | Measurement | Default threshold |
| --- | --- | --- |
| `detect_rating_drift` | TVD of exact numeric rating categories, inclusive range 1–10 | 0.20 |
| `detect_event_type_drift` | TVD of `watch`, `rating`, `account_created` proportions | 0.20 |
| `detect_missing_value_drift` | Absolute missing-rate difference, one result per selected field | 0.10 (10 percentage points) |

TVD is `0.5 * sum(abs(P_baseline(category) - P_recent(category)))`, over the
union of categories. An absent category has probability zero. Scores lie in
[0, 1]; zero means identical proportions and one means disjoint distributions.
Count ratios are computed with exact rational arithmetic before converting the
final score to float, avoiding subtraction artifacts at common threshold edges.
Both snapshots use their own denominator, so different window sizes are allowed.

The minimum is **100 records in each snapshot** by default. Rating inputs must
contain only rating measurements, so this means 100 rating events per window,
not 100 mixed events. Event-type comparisons require 100 total events per window.
Missingness uses all raw records supplied, including absent/null fields. Override
`min_samples` with a positive integer and `threshold` with a finite number in
[0, 1]. These are **initial heuristics**, not significance tests: 100 avoids
decisions from a handful of observations, TVD 0.20 flags substantial probability
mass changes, and 0.10 flags operationally noticeable missingness changes.
Calibrate all three policies using real traffic, normal seasonal variation and
the team's acceptable false-alert rate.

### Results and invalid data

Each result dictionary contains `metric`, `baseline_size`, `recent_size`,
`drift_score`, `threshold`, `min_samples`, `status`, and structured `errors`.
Missingness returns a list in the requested field order, with metric names such
as `missing_value_rate:movie_id`. Categorical names are
`rating_distribution_tvd` and `event_type_distribution_tvd`.

- `drift_detected`: enough valid measurements and score **>=** threshold.
- `no_drift`: enough valid measurements and score below threshold.
- `insufficient_data`: either snapshot is empty or below the minimum; score is
  `None`, never a misleading zero. Errors are empty.
- `invalid_data`: malformed snapshot/record or measurement; score is `None`.
  This takes precedence over insufficient samples. Errors identify the snapshot,
  row index and measurement field without embedding raw data. Row counts include
  invalid rows; no rows are silently dropped or treated as extra categories.

Invalid configuration raises `ValueError`, rather than reporting a data status.
At threshold zero, even identical distributions meet the inclusive threshold;
use a positive threshold for normal monitoring.

Rating measurements must be native numeric values (int, float or Decimal), finite
and in [1, 10]; booleans, numeric strings, missing keys and nulls are invalid.
Fractions are valid because Nathan's schema permits float ratings. Each distinct
float value is a category; Decimal inputs are converted to float, with no hidden
rounding/bins. Event types require exact schema literals without whitespace or
case coercion. These functions validate their measurements, not the entire event
schema; unrelated fields need separate boundary validation.

Missing means **absent key or explicit `None`**. Zero, false, empty strings and
collections count as present. Numeric NaN/Infinity are invalid, not substitutes
for null. Missingness does not assert other field types or validity: for example,
the literal string `"NaN"` counts as present. A field absent from every record is
a measured 100% missing rate. Monitor schema rejections separately, and select
fields by event type when nulls are expected (for example, rating is normally
null for watch/account events). This supports raw pre-validation records without
discarding the very missingness being measured.

### Usage and snapshot integration

```python
from src.data_quality.drift import (
    detect_rating_drift, detect_event_type_drift, detect_missing_value_drift,
)

baseline = [{"event_type": "rating", "rating": 5, "movie_id": "test-movie"}] * 100
recent = [{"event_type": "rating", "rating": 9, "movie_id": None}] * 100
ratings_result = detect_rating_drift(baseline, recent)
types_result = detect_event_type_drift(baseline, recent)
missing_results = detect_missing_value_drift(baseline, recent, ["movie_id"])
# ratings_result["status"] == "drift_detected", score 1.0
# types_result["status"] == "no_drift", score 0.0
# missing_results[0]["status"] == "drift_detected", score 1.0
```

Nathan's verified read interface is
`src/database/interactions/stream_events.py:search(start=..., end=...,
time_basis="event_time", event_type=..., cursor=..., limit=...)`. It returns
`SearchPage.records` and `SearchPage.next_cursor`, newest first. Windows are
half-open (`start <= time < end`); boundaries must be timezone-aware, and a page
limit is at most 1,000. Integration code outside the pure detector should:

1. Select explicit, nonoverlapping UTC windows and record their boundaries and
   filters with the result. An initial candidate is a recent completed 24 hours
   against the preceding 7 completed days. This is an example policy, not a
   hard-coded default; match weekdays/seasonality and ingestion delay as needed.
2. For each window, exhaust `next_cursor` with unchanged filters and boundaries,
   collect `row.model_dump()` mappings, and materialize a stable snapshot. A
   single page is not a complete window. Consider delayed events arriving during
   paging; use completed windows and an ingestion cutoff/snapshot policy owned
   by the integration layer. Choose `event_time` or `stored_at` consistently.
3. Query `event_type="rating"` for rating drift. Query all events for event-type
   drift. Use equivalent population filters across windows; do not compare a
   rating-only window with an all-event window. This detector never silently
   filters mixed input: null ratings will make a rating comparison invalid.
4. For pre-validation missingness, supply raw decoded ingestion snapshots from
   the consumer owner, including records rejected by boundary validation. Stored
   events only represent accepted data and cannot reveal missingness among
   discarded records. Do not normalize nulls or silently remove invalid rows.
5. Keep snapshots replay-safe (deduplicate by stable event identity upstream),
   inspect result statuses/errors, and choose reporting/alert handling outside
   this module. No production fetches, monitoring job, CI changes or scheduler
   are added here.

Limitations: TVD is descriptive, with no confidence intervals or causal claim.
Exact fractional categories can exaggerate drift from tiny numeric changes;
future binning requires an explicit shared policy, not silent rounding. Minimum
counts do not guarantee representative independent samples, and repeated watch
events or changes in user mix can change proportions. Missingness changes may
reflect event mix rather than ingestion failures. Results are invariant to row
order for valid snapshots; invalid row error locations follow the supplied
order. The core materializes snapshots and is intended for bounded M2 windows,
not unbounded streams. Baseline selection, persistence of reports, alerting and
threshold calibration remain integration responsibilities.

## Data Quality logging

`src/data_quality/quality_logging.py` uses standard Python logging and emits one
JSON **message** per call. It adds no handlers, formatters, root configuration,
logger levels, scheduled jobs or global counters. The default logger is
`src.data_quality.quality_logging`; callers can pass their own `logging.Logger`.
Existing validators and detectors remain pure and never log automatically.

```python
log_validation_failure(
    source: str, errors: Sequence[Mapping], *,
    rejected_record_count: int = 1, logger: logging.Logger | None = None,
    sink: QualityLogSink | None = POSTGRES_SINK, context: LogContext | None = None,
) -> None

log_drift_result(
    result: Mapping[str, object], *, logger: logging.Logger | None = None,
    sink: QualityLogSink | None = POSTGRES_SINK, context: LogContext | None = None,
) -> None
```

Validation sources are restricted to `kafka`, `user_api`, `movie_api`, and
`recommendation_api`. Pass the list (or tuple) of structured errors returned by
the boundary validator, not the raw rejected record. Empty errors produce no
message. One call normally represents one rejected record with potentially many
errors; when combining records, explicitly supply the actual positive integer
`rejected_record_count`. Do not equate error count with rejected record count.

Validation messages contain:

- `source`: the fixed boundary label.
- `issue_type`: `validation_failure`.
- `validation_failure_count`: number of validation error entries in this call.
- `rejected_record_count`: records rejected, default 1.
- `errors`: sanitized issues, each with `field`, `location` and
  `validation_error_type`. Root errors use field `$`; for example a nested item
  field is `recommendations.<index>.movie_id`. `location` is the corresponding
  JSON array of sanitized path components.

Drift messages contain `source: "drift"`, `issue_type: "drift_result"`, `metric`,
`baseline_size`, `recent_size`, `drift_score`, `threshold`, `min_samples`, `status`,
`validation_failure_count`, and sanitized `errors`. They accept existing detector
results unchanged; unevaluated results retain a null score. No errors or input
fields are copied wholesale. Additional input keys are ignored. Numeric scores
and thresholds must be finite and in [0, 1]; counts must be nonnegative integers,
and `min_samples` must be positive. Only the existing detector metric names and
statuses are supported. Missingness field names outside the safe field list are
reported as `missing_value_rate:<redacted>`.

| Result | Level | Policy |
| --- | --- | --- |
| Validation failure | WARNING | Rejected boundary input needs attention |
| `drift_detected` | WARNING | Distribution change crossed the configured threshold |
| `no_drift` | INFO | Normal completed comparison |
| `insufficient_data` | INFO | No reliable conclusion yet; not a drift alert |
| `invalid_data` | ERROR | Comparison blocked by malformed measurements |

### Privacy and safe error paths

Only schema field names and fixed path labels are retained. All numeric path
indices become `<index>`; arbitrary keys/objects become `<redacted>`. This matters
because Pydantic extra-field locations and unsupported query parameter keys may
contain user-supplied text, including IDs or secrets. Known Pydantic error codes
are retained; unknown custom error types are redacted. The helpers never
serialize `msg`, `input`, `ctx`, URLs, exception objects, raw IDs, profiles,
preferences, credentials, records or responses. They do not stringify unknown
objects, even for error paths. Raw messages are intentionally excluded because
custom validators can embed sensitive values in their messages.

### Kafka and Flask integration

```python
from src.data_quality.boundary import validate_stream_event
from src.data_quality.quality_logging import log_validation_failure, log_drift_result

validated, errors = validate_stream_event(raw_event)
if errors:
    log_validation_failure("kafka", errors)
    # Consumer owner chooses rejection/retry behavior; do not also log raw_event.

# After a pure drift comparison:
log_drift_result(ratings_result)
```

Flask owners use the same pattern with `user_api`, `movie_api`, or
`recommendation_api` after their validator calls, and separately implement HTTP
error handling. Log once per validation decision; avoid recounting retries as
distinct records unless that is the intended reporting policy. Use the drift
logger once per metric/window result, including every missingness result.

Handlers, output destinations, timestamps and retention are application-owned.
If the log collector requires every output line to be JSON, the owner must use
a message-only formatter (`%(message)s`) or a compatible structured formatter;
an existing text-prefix formatter will wrap the JSON message in non-JSON text.
The helper never changes that formatter or adds a handler. Preexisting duplicate
handlers/propagation can still duplicate output and must be configured by the
application owner. No new logging dependency is required.

Malformed logger inputs raise `TypeError`, `ValueError` or `KeyError`; unexpected
errors outside the best-effort sink propagate. Sink failures emit a sanitized
fallback diagnostic instead of interrupting ingestion/serving. The logger does not recompute drift decisions
or repair invalid result structures. Redaction deliberately reduces detail for
unknown fields and row indices. Counts are per call, not persisted totals, rates,
or unique-event metrics; aggregation, alert routing and rate limiting belong to
the integration owner. Payload size scales with the number of supplied errors.
These helpers cannot prevent sensitive data being logged by other service code.

## Modular database reading and drift orchestration

`src/data_quality/database_reader.py` wraps only Nathan's supported public
interfaces: `stream_events.search`, `recommendation_serves.search`, and
`llm_usage.search` in `src/database/interactions/`. No SQL, engine/session logic,
table mutations, or new database interfaces are introduced.

```python
reader = DatabaseReader(
    event_search=None, serve_search=None, usage_search=None,
    page_size=1000, logger=None,
)
```

Unspecified search functions are imported lazily when that source is read. For
tests/offline fixtures, inject callables with the corresponding public `search`
signature returning an object with `records` and `next_cursor` (normally Nathan's
public `SearchPage` and `SearchCursor`). Merely constructing a reader performs
no database work. Production reads use the existing service dependencies in
`requirements-service.txt` and Nathan's configured database credentials. No new
packages or configuration settings are needed.

### Reader interfaces

All read methods return `list[dict]` in database newest-first order and require
timezone-aware datetime `start` and `end` keyword arguments:

| Method | Verified optional filters / time basis |
| --- | --- |
| `read_events` | `event_type=None`, `time_basis="event_time"`, `user_id=None`, `movie_id=None` |
| `read_watch_events` | Same event filters except fixed `event_type="watch"` |
| `read_rating_events` | Same event filters except fixed `event_type="rating"` |
| `read_recommendation_serves` | `user_id=None`, `model_route=None`, `model_version=None`; time basis is `served_at` |
| `read_llm_usage` | `user_id=None`, `provider=None`, `model_name=None`, `status=None`; time basis is `occurred_at` |

Event reads also support `time_basis="stored_at"`. `read_events(event_type=None)`
includes all event types, including account creation. Filters are passed to the
database, never applied by silently discarding returned rows. Unknown keywords,
invalid event/time-basis choices, blank/non-string filters and invalid windows
raise before a query. Window boundaries are normalized to UTC; the existing
database API applies `start <= timestamp < end`. Unbounded windows are deliberately
not exposed. `page_size` is an integer from 1 to 1,000.

Every page is retrieved until `next_cursor is None`, even when a page has fewer
than `page_size` records. Filters/boundaries remain unchanged. Repeated cursors,
an empty page with a continuation, malformed page structure or an unconvertible
row raise `DatabaseReadError`; no partially collected list is returned. An empty
completed read returns `[]`. There is no hidden page or row cap.

SQLModel/Pydantic rows use `model_dump(mode="python")`; injected mapping rows
are deep-copied. No identifiers, timestamps, ratings, nulls or telemetry fields
are dropped, normalized or synthesized. In particular, serve data keeps
`request_id`, `served_at`, `request_received_at`, `serve_duration_ms`, model
route/version, recommendation ordering and optional item scores. Usage data keeps
`request_id`, `occurred_at`, provider/model/status, token counts and exact Decimal
`estimated_cost_usd`. The reader does not invent an additional prediction log,
latency, cost field or cross-table join.

These dictionaries can contain sensitive data and must stay in the evaluation
pipeline, not logs. A measurement like `rating=None` or an unknown event type is
retained for the drift detector to report as `invalid_data`; the reader does not
revalidate an entire stored row or silently repair it. Stored `stored_at` and
other table fields are intentionally retained, so a table dictionary is not
directly a strict `StreamEventCreate` payload (which forbids extra fields).

### Snapshot and evaluation examples

```python
from datetime import UTC, datetime
from src.data_quality.database_reader import (
    DatabaseReader, read_event_snapshots, detect_database_drift,
)

reader = DatabaseReader()
windows = dict(
    baseline_start=datetime(2026, 1, 1, tzinfo=UTC),
    baseline_end=datetime(2026, 1, 8, tzinfo=UTC),
    recent_start=datetime(2026, 1, 8, tzinfo=UTC),
    recent_end=datetime(2026, 1, 9, tzinfo=UTC),
)
snapshots = read_event_snapshots(reader, **windows, event_type="rating")
# {"baseline": [...], "recent": [...]}, ready for detect_rating_drift(**snapshots)
results = detect_database_drift(reader, **windows, threshold=0.20, min_samples=100)
# {"rating": DriftResult, "event_type": DriftResult}; both results are logged.

serves = reader.read_recommendation_serves(
    start=windows["recent_start"], end=windows["recent_end"], model_version="test-version",
)
usage = reader.read_llm_usage(start=windows["recent_start"], end=windows["recent_end"])
# Evaluation owners explicitly join/correlate using verified identifiers.
```

`read_event_snapshots` requires `baseline_end <= recent_start`; touching
half-open windows are allowed, gaps are allowed, overlaps and reversed chronology
are rejected before reads. Both snapshots use the same event type and time basis.
`detect_database_drift` first validates detector configuration, then makes four
complete reads: baseline/recent with `event_type="rating"`, followed by
baseline/recent with no event-type filter. It calls the unchanged pure detectors
and logs results through `quality_logging.log_drift_result`. It returns their
existing structures without wrapping database failures as `no_drift`, empty data,
or `insufficient_data`. Failed reads raise; result logging happens only after all
four reads and both comparisons complete successfully (detector `invalid_data`
is a completed comparison status, not a database failure).

Missing-value drift is intentionally **not** computed from these reads by default:
pre-validation raw snapshots, including rejected records, must come from the
Kafka/API ingestion owner. Database-only missingness cannot measure rejection
rates. Owners can still call the pure missingness detector for explicitly scoped
accepted-record analyses and label that population accurately.

### Read/processing logging and failure policy

The reader emits INFO JSON messages with fixed source labels (`stream_events`,
`recommendation_serves`, `llm_usage`), `issue_type="database_read"`, and operations
`read_started`, `page_processed`, `read_completed`. Counts are cumulative for that
read: `records_fetched`, `records_processed`, and, after fetching starts,
`pages_read`. Page/completion messages include `pagination_complete`. Processing
here means conversion to dictionaries, not acceptance by a schema validator.

SQLAlchemy database failures emit ERROR with operation `database_read_failed`;
malformed pages/records emit ERROR with `invalid_page_or_record`. Both include
progress counts and `pagination_complete=false`, and re-raise without returning
partial data. SQLAlchemy exceptions are preserved for caller retry policy; do
not log their messages/tracebacks, which can contain credentials or bound values.
Configuration errors (such as Nathan's missing credential RuntimeError) and
unexpected programming errors propagate normally. No blanket exception swallowing
or retry policy is added.

Drift orchestration emits an INFO `drift_processing` message per metric with
operation `comparison_completed` and `records_processed`, then delegates all
drift score/status logging to the existing quality logger. Read logs never include
record contents, filter values, IDs, model names/versions, cursor contents,
credentials or database exception text. There are no added handlers or global
logging changes. Callers own sinks, JSON line formatting and error responses.

### Integration limitations

Nathan's searches open separate transactions per page and per query: exhausting
pagination does not create an atomic cross-page/cross-table production snapshot.
Use completed windows and an ingestion-cutoff/export policy; concurrent late
inserts can otherwise affect reproducibility. The reader preserves duplicate rows
if an upstream source supplies them; it never silently deduplicates evaluation
data. Stable immutable event/request IDs support upstream replay audits.

All rows are materialized in memory, suitable for bounded M2 windows rather than
unbounded history. There is no automatic label/serve join, atomic snapshot API,
raw-rejection table, cost computation, metric aggregation, retry scheduler or
monitoring service here. Existing LLM usage `request_id` and serve `request_id`
are preserved but equality is not assumed to guarantee a correlation unless
ingestion owners establish that contract. Offline/online evaluation owners must
choose population/window filters, define correlation and late-data handling,
calibrate drift thresholds, and call these helpers from their own workflow.

## Historical test coverage (before PostgreSQL log persistence)

Measured on 2026-11-03 (local time) using Python 3.12.9 and coverage.py 7.17.1.
CI's existing command is `python -m unittest discover -s src/tests -v`, with
dependencies from `src/requirements-service.txt`. There was no existing coverage
configuration or coverage dependency. Coverage was installed only into the
temporary test virtual environment; production requirements and CI are unchanged.
Nathan's separate in-memory database tests were also run and appended to coverage.
Archived experiment tests under `planning/` and HTTP/Docker smoke scripts are not
part of these unittest discovery commands.

Commands actually used, from the repository root (the temporary Python 3.12
environment already contained the pinned service dependencies):

```sh
/tmp/m2-boundary-venv/bin/python -m pip install coverage

COVERAGE_FILE=src/data_quality/.coverage PROFILE_DB_PATH=/tmp/m2-boundary-venv/test-state/profiles.sqlite3 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 /tmp/m2-boundary-venv/bin/python -m coverage run --branch --source=src/data_quality -m unittest discover -s src/tests -v

COVERAGE_FILE=src/data_quality/.coverage /tmp/m2-boundary-venv/bin/python -m coverage run --append --branch --source=src/data_quality -m unittest discover -s src/database/tests -v

COVERAGE_FILE=src/data_quality/.coverage /tmp/m2-boundary-venv/bin/python -m coverage report -m --precision=2 --fail-under=80

COVERAGE_FILE=src/data_quality/.coverage /tmp/m2-boundary-venv/bin/python -m coverage html -d src/data_quality/htmlcov

git check-ignore src/data_quality/.coverage src/data_quality/htmlcov/index.html src/data_quality/coverage.json
```

For a fresh environment, first create a Python 3.12 virtual environment and
install `-r src/requirements-service.txt`; substitute its Python executable for
the temporary path above. Pin `coverage==7.17.1` when reproducing this measurement.
`PROFILE_DB_PATH` redirects existing Flask import-time SQLite initialization to
a writable temporary path. The model integration tests use recorded local
fixtures and temporary model artifacts. The database suite uses in-memory SQLite;
none of these commands connects to production PostgreSQL, Kafka or external APIs.

Initial coverage was **99.65% combined statement/branch coverage** with 141 main
tests passing. The only missing executable branch was rejecting a non-string
drift metric in `quality_logging.py`. Added
`test_non_string_drift_metric_is_rejected_without_serialization`, covering null,
numeric, mapping and unprintable-object metric inputs; it asserts rejection
without emitting a log or stringifying sensitive objects. No production behavior
was modified to improve coverage.

Final results: **142/142 main tests passed** (including 128 Data Quality tests),
plus **13/13 database tests passed**, totaling **155 successful test executions**.

| Data Quality file | Statements | Missed | Branches | Partial branches | Statement coverage | Branch coverage | Combined coverage |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `__init__.py` | 0 | 0 | 0 | 0 | 100.00% | N/A | 100.00% |
| `boundary.py` | 103 | 0 | 52 | 0 | 100.00% | 100.00% | 100.00% |
| `database_reader.py` | 120 | 0 | 42 | 0 | 100.00% | 100.00% | 100.00% |
| `drift.py` | 102 | 0 | 40 | 0 | 100.00% | 100.00% | 100.00% |
| `quality_logging.py` | 73 | 0 | 34 | 0 | 100.00% | 100.00% | 100.00% |
| **Total** | **398** | **0** | **168** | **0** | **100.00%** | **100.00%** | **100.00%** |

The terminal report's `Cover` column combines statements and branches because
`--branch` was enabled. No measured statements or branch destinations remain
uncovered, and the 80% check passed. This is Data Quality coverage, not a claim
of full-repository coverage. Exercising every branch does not prove every input
combination or production behavior: real PostgreSQL failures/concurrency, live
API contracts, Kafka replay behavior, log collector configuration, large-window
memory use and real-data threshold calibration remain integration concerns.

The generated report is `src/data_quality/htmlcov/index.html`; it is a local,
regenerable artifact. The repository root `.gitignore` excludes `.coverage*`,
`htmlcov/`, and optional `coverage.json` throughout the repository. Ignore matching was
verified with `git check-ignore`; no generated coverage output is intended for
Git. Keep future coverage commands pointed at these paths (or another explicitly
ignored artifact directory).

## PR review update: database-owned validation and PostgreSQL log storage

The current `src/service/course_stream.py` consumes course **text** messages for
account/profile jobs and recommendation evidence in `ProfileStore`. It does not
yet write decoded stream events through Nathan's PostgreSQL interaction API.
No duplicate-validator call chain actually exists in that service today. Its text
parser, SQLite behavior, offset commits and retry policy remain unchanged; no
unverified watch/rating text format or automatic PostgreSQL migration is added.

### One validation at the ingestion handoff

`src/data_quality/ingestion.py:persist_stream_event(raw_event, *, create_many=None,
logger=None, sink=POSTGRES_SINK, context=None)` is the handoff for the consumer
owner's decoded event writer. It calls the **public**
`stream_events.create_many([raw_event])` directly. That API performs Nathan's
`StreamEventCreate.model_validate()` exactly once per raw event, then executes
its existing replay-safe insert. No pre-call to `validate_stream_event()` or
round trip from validated model back to dictionary is needed. The standalone
boundary validator remains available for offline/non-persistence callers;
`stream_events.create()` and `create_many()` keep their existing validation for
every other database caller. No trusted/unvalidated database write API is added.

```python
from src.data_quality.ingestion import persist_stream_event

inserted, errors = persist_stream_event(decoded_event)
if errors:
    # Already logged safely. Consumer owner chooses rejection evidence/commit policy.
    pass
# inserted == 1: stored; inserted == 0: existing deterministic event UUID.
# inserted is None: input rejected. Processing a later event is still safe.
```

Only Pydantic input errors are caught and converted to `(None, errors)`; errors
exclude raw input and context and are logged with source `kafka`. Database
connection/integrity/transaction failures propagate so the consumer must **not**
advance offsets for a failed event write. Log-storage failures are separately
best-effort and never turn an input rejection into a consumer exception. Supply a
stable `event_id` from a verified ingestion identity for replay safety; an omitted
ID follows the database's existing random UUID behavior. Parent user/movie rows
must exist for accepted events. No parser, identity or parent rows are invented.
The hook is covered with real repository writes on in-memory SQLite and a spy
asserting one `StreamEventCreate.model_validate()` call. Wiring this hook after
the consumer owner's verified decoding step is still required; it is not wired
into the existing unrelated text/ProfileStore path.

### Storage architecture

Both public logging functions now default to `POSTGRES_SINK`, a stateless
`PostgresQualityLogSink` implementing `QualityLogSink.write(record)`. JSON
messages are still emitted first through Python logging with the same sanitized
schema. The sink receives a detached JSON-safe record containing only approved
fields. `sink=None` explicitly selects JSON-only logging; tests or a future Loki
implementation can inject another sink without changing validators/detectors.
There are no global handlers, backend environment toggles or import-time reads.

`src/database/models.py:DataQualityLog` registers the append-only
`data_quality_logs` table with Nathan's existing SQLModel metadata:

- `log_id` UUID primary key and indexed UTC `occurred_at` timestamp.
- Indexed `source`, `severity`, `issue_type` labels.
- `error_details` JSONB containing sanitized field/error-code entries, never
  Pydantic `msg`, `input`, exception context, credentials or raw payloads.
- `log_metadata` JSONB containing sanitized counts or drift metric/score/status
  data already selected by `quality_logging`; arbitrary additional metadata is
  not accepted by the public logging helpers.
- Optional indexed `user_id`, `movie_id`, `event_id`, `request_id` for explicit
  debugging correlation. These deliberately have **no foreign keys**: rejected
  events may reference nonexistent rows. IDs are nullable and never inferred
  from an untrusted rejected payload.

```python
from src.data_quality.quality_logging import LogContext, log_validation_failure

log_validation_failure(
    "recommendation_api", errors,
    context=LogContext(request_id=verified_request_uuid),
)
# Optional user_id/movie_id/event_id may be supplied from trusted request context.
# Context IDs go only to database columns, never JSON messages or fallback logs.
log_validation_failure("kafka", errors, sink=None)  # portable JSON-only mode
```

Correlation IDs are sensitive operational data: supply only those necessary for
debugging and limit database access/retention accordingly. The repository's
low-level `create` accepts structured JSON, so direct repository callers are
responsible for the same sanitization policy; application code should use the
quality logger instead of passing arbitrary dictionaries to persistence.

`src/database/interactions/data_quality_logs.py` uses the shared `run`,
`create_record`, `schema_values`, session lifecycle and pagination helpers:

```python
from src.database.interactions import data_quality_logs

page = data_quality_logs.search(
    start=start_utc, end=end_utc, source="kafka", severity="WARNING", limit=100,
)
# Use page.next_cursor with identical filters to retrieve subsequent pages.
row = data_quality_logs.get(log_id)
rows = data_quality_logs.list_records(offset=0, limit=100)
```

Search also supports issue type and optional correlation-ID filters. Timestamp
windows are half-open; reads follow existing newest-first timestamp/UUID cursor
ordering. Each write uses a separate short transaction from event persistence;
failed log writes roll back without poisoning the event/request transaction.
The existing `python -m src.database.initialize` command creates the additional
table through metadata `create_all`; no destructive migration or new connection
logic is introduced. Deployment owners must run that existing initialization step
before expecting database logs. No production DDL was executed during this task.

### Failure and operational limits

If any sink write raises an ordinary Exception, the original sanitized JSON
entry remains in the Python log and one ERROR JSON message with issue type
`quality_log_persistence_failure` and `fallback="python_logging"` is emitted.
Exception strings, SQL/bind parameters, tracebacks and correlation IDs are never
included. There is no recursive persistence attempt. Invalid caller options
(bad sink/context) raise before emission; errors raised by Python handlers and
process interrupts are not hidden. Sink bugs are made visible by the same fallback
instead of crashing a recommendation request or Kafka input-rejection handler.

Persistence is synchronous/best-effort, not a durable queue or an atomic part of
the event write. It can add database latency and remains subject to Nathan's
existing connection/pool timeouts. No automatic retries, circuit breaker,
retention cleanup, exactly-once log semantics, Loki client or scheduled service is
added. Integration owners should monitor fallback diagnostics and set operational
timeouts/retention; a future asynchronous/Loki sink can replace this implementation.

### Review-update test results

New tests are in `src/tests/test_data_quality_persistence.py`. They use in-memory
SQLite via the existing repository engine injection, not live PostgreSQL or
Kafka. PostgreSQL JSONB/UTC timestamp DDL is compiled as an additional dialect
check; this is not a claim of a live PostgreSQL deployment test. Tests cover
append/read/search, IDs, pagination, all drift statuses, missing-table rollback,
privacy, pluggable/disabled sinks, malformed Kafka inputs, single validation,
duplicate UUID inserts, preserved validation for other callers, and sink failures.

Executed on 2026-11-05 using Python 3.12.9:

- New persistence/ingestion tests: **18 passed**.
- All Data Quality tests: **146 passed**.
- Full `src/tests` suite: **160 passed**.
- Separate `src/database/tests` suite: **13 passed** (173 full-suite executions).
- Updated Data Quality statement/branch coverage: **100.00%**, 445 statements
  and 178 branches, including the new ingestion module. HTML report regenerated
  at the existing ignored `src/data_quality/htmlcov/index.html` path. The earlier
  coverage table above is retained as the historical pre-review measurement.

Commands (from the repository root, using the existing temporary environment):

```sh
/tmp/m2-boundary-venv/bin/python -m unittest discover -s src/tests -p 'test_data_quality_persistence.py' -v

PROFILE_DB_PATH=/tmp/m2-boundary-venv/test-state/profiles.sqlite3 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 /tmp/m2-boundary-venv/bin/python -m unittest discover -s src/tests -p 'test_data_quality*.py' -v

COVERAGE_FILE=src/data_quality/.coverage PROFILE_DB_PATH=/tmp/m2-boundary-venv/test-state/profiles.sqlite3 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 /tmp/m2-boundary-venv/bin/python -m coverage run --branch --source=src/data_quality -m unittest discover -s src/tests -v

COVERAGE_FILE=src/data_quality/.coverage /tmp/m2-boundary-venv/bin/python -m coverage run --append --branch --source=src/data_quality -m unittest discover -s src/database/tests -v

COVERAGE_FILE=src/data_quality/.coverage /tmp/m2-boundary-venv/bin/python -m coverage report -m --precision=2
COVERAGE_FILE=src/data_quality/.coverage /tmp/m2-boundary-venv/bin/python -m coverage html -d src/data_quality/htmlcov
```
