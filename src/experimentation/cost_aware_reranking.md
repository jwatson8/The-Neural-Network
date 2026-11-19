# Cost-aware Reranking

This Treatment reranks the existing model's candidates using `movies.license_cost`.
These are catalog reference prices, in the catalog's monetary units; the column
has no currency field. Actual experiment outcomes must use `licensing_costs`, the
watch-derived ledger. Reference-price diagnostics are not actual savings.

## Candidates and Relevance

`LoadedModelCandidates(request, limit)` follows Control's routing: SVD only with
current history and the trained ten-movie threshold, otherwise saved-profile
content scores, otherwise popularity. Empty saved profiles also use popularity.
The API supplies `has_history` and live history exclusions once. The source also
excludes the bundle's seen movies. Missing profiles queue the existing profile job;
profile read/scoring errors instead make Treatment unavailable.

The shared score helpers expose existing calculations without changing Control's
K limit or selection behavior:

- SVD: the actual user-factor/movie-factor dot product. It is a latent relevance
  signal, not a predicted 1–10 rating or calibrated probability.
- Content: positive TF-IDF match minus 0.7 times negative match plus 0.02 times the
  existing popularity value. It is an existing heuristic, not a fabricated rating.
- Popularity: the actual trained bundle's popularity array (training derives it
  from log positive-interaction counts). It has no per-user preference information.

Scores are not compared across routes. Within a candidate pool, normalize relevance
as `1 - average_tied_rank / (N - 1)`, ranks starting at zero; a single candidate
has relevance 1. Tied scores share their average rank. This monotonic percentile
proxy preserves the raw score ordering at lambda zero and avoids mixing incompatible
score magnitudes. Magnitude information is intentionally lost.

The default pool is `min(5 * K, 200)`; the API still returns at most 20. Scoring
uses the same full-catalog computations as Control, with one sort; at most 200
candidates/prices reach reranking. It does not add per-movie transforms, LLM calls
or DB queries. Candidate IDs must be syntactically valid, distinct, unseen, and in
both the model catalog and the batched database catalog result. Nonfinite scores
are dropped. If fewer than K valid candidates survive, use Control without exposure.
No unbounded retries refill the pool. Reranking exactly K candidates cannot reduce
their total reference cost; an expanded pool permits, but does not guarantee, changes.

## Cost Penalty

```text
adjusted_score = percentile_relevance - lambda * normalized_cost
normalized_cost = price / (price + lower_median_positive_price)
```

Both terms are dimensionless, with relevance in [0,1] and cost in [0,1]. The lower
median of positive known prices supplies the scale, making minority extreme
outliers less influential than a max-price divisor. Ratios are evaluated with
Decimal arithmetic without adding huge prices. Known zero is 0; an all-zero known
catalog is valid. Equal prices have equal penalties without division by zero.

Null, malformed, negative, boolean and nonfinite prices are unknown and receive
penalty 1. They are never treated as free. They may still be selected for relevance,
but never receive a cost advantage. A missing DB key means a nonexistent catalog
movie and is excluded entirely. If no valid known price exists, Treatment fails
closed. `movies.get_license_costs()` reads IDs/prices in one SELECT, with a hard
500-ID public limit; this adapter sends at most 200. No outcome-ledger reads occur.

Default lambda is **0.15**, a conservative starting cap of 15 percentile points,
not an optimized or empirically validated setting. Values must be finite in [0,1].
Freeze lambda, pool settings and the model pin before any live experiment. Final
ties use relevance then original stable candidate order. Never infer satisfaction
or financial improvement from adjusted scores.

## Adapter and Provenance

`CostAwareTreatmentServer(candidate_source, model_version, config, catalog_lookup)`
implements the existing injectable `TreatmentServer` call. It accepts only assigned
Treatment decisions for policy `cost_aware_rerank` and its own version token.

Use `treatment_version(actual_model_version, CostAwareConfig(...))` to obtain the
experiment's `treatment_version`: `cost-aware-rank-v1-<configuration hash>`. This
fingerprints the algorithm, lambda, pool settings and independent trained-model
pin. The adapter never copies an assignment version into actual model metadata.
Configuration/model changes produce a mismatch and normal-serving fallback.
When provisioning a draft experiment, also retain the resolved weight, pool settings,
model pin and algorithm identifier in `experiments.config` for human review and
reproduction. The adapter validates its fingerprint rather than reading mutable
JSON parameters per request; it never activates or updates an experiment.

`ServingResult` adds optional, backward-compatible `policy_version` and
`underlying_route` fields. When present, policy_version matches the assignment's
version; otherwise existing adapters keep the previous model-version contract.
Actual `model_version` always identifies the underlying bundle. Audits store routes
such as `cost_aware:collaborative_svd`, `cost_aware:llm_content`, or
`cost_aware:popularity_fallback`. Intentional popularity candidate generation is a
fulfilled cost-aware policy, not a failed Treatment. Policy version and underlying
route also appear in structured logs; no database migration is needed.

Alvajoy can wrap an immutable hosted bundle in the scored `CandidateSource` protocol
or inject the whole TreatmentServer. Construct one adapter per actual model pin;
do not mutate its model bundle in place. A future source must preserve real scores,
route identity, stable order, exclusions and the 200-candidate bound. Hosted-version
resolution itself is not implemented here.

## Explicit Flask Wiring

Both `EXPERIMENTS_ENABLED` and `COST_AWARE_TREATMENT_ENABLED` default to False.
The latter registers this adapter only for an enabled, explicitly pinned experiment
and an available loaded model. It does not create, start or select experiments.
An injected adapter and built-in registration cannot both be configured.

Factory configuration accepts `COST_AWARE_WEIGHT` (default "0.15"),
`COST_AWARE_CANDIDATE_MULTIPLIER` (5), and `COST_AWARE_MAX_CANDIDATES` (200).
Alternatively construct/inject `CostAwareTreatmentServer` directly. No Compose or
environment setting has been changed. Existing Control behavior remains intact.

Missing model/source, empty/insufficient valid pools, absent price evidence, lookup
failure, ranking failure, unsupported policy or version mismatch all use the existing
router safety behavior. Control fallback has its real route/version and NULL
experiment exposure FK. Existing assignments remain for intention-to-treat analysis.
Audit errors still return 503. Server-side audit still cannot prove client delivery.

## Offline Diagnostics and Sensitivity Analysis

From the repository root, evaluate the full grid without connecting to a database:

```sh
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python -m src.experimentation.cost_diagnostics \
  --dataset recorded --sample-size 100 --seed 42 --k 20 --json /tmp/cost-recorded.json
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python -m src.experimentation.cost_diagnostics \
  --dataset synthetic --sample-size 100 --seed 42 --k 20 --json /tmp/cost-synthetic.json
python -m pytest src/tests/test_cost_sensitivity.py src/tests/test_cost_reranking.py \
  src/tests/test_cost_aware_treatment.py
```

`cost_sensitivity` is also a direct module entry point. The fixed lambda grid is
0.00, 0.05, 0.10, 0.15, 0.20, 0.30, 0.50, 0.75, 1.00. `--sample-size` limits users
per route (1..1000); `--k` accepts 1..20. Every user has one frozen expanded pool
`min(5*K,200)` shared by all lambdas, including identical scores, prices and history
exclusions. The tool calls the existing `rerank()` implementation; no duplicate
ranking algorithm is implemented. Readable tables print by default; `--format json`
or `--json -` produces JSON-only stdout, and `--json PATH` explicitly exports JSON.
Nothing is written unless an output path is supplied; no experiment reports or
production configuration are created or updated.

### Sources and Reproducibility

The default `recorded` dataset loads the trusted checked-in M0 artifact from
`planning/Milestone1/modelTesting/full_models/jace_model.joblib`, public-course
compressed catalog/history/user files, and the saved profile JSON. No production
artifact, DB, credentials, network download, or LLM call is used. CSV checksums must
match `data_manifest.json`; profile description hashes and model/catalog IDs are
checked. Missing/changed recorded inputs fail explicitly rather than silently
substituting synthetic prices. The artifact has no embedded model version, so its
SHA-256 identifies it; no version is invented.

Recorded data contains 2,569 priced catalog movies, 1,050 users, 54,704 events and
50 assistant-authored preference summaries of supplied descriptions. Eligible route
populations are 998 SVD, 50 content and only two popularity users. With sample limit
100/seed 42/K=20 the evaluation covers 100/50/2 users, each with 100 valid-priced
candidates. Distinct candidate rankings are 100/50/2. This is historical course
metadata and in-sample model relevance, not a representative current-production
sample or observed licensing charges.

`synthetic` fits an in-memory model to seeded synthetic ratings/documents and varies
profiles, history exclusions and reference prices. The sample limit controls users
per route. It labels all generated inputs explicitly, includes unknown/zero prices,
and provides broader popularity stress coverage without pretending those users or
prices were observed. The original `synthetic_diagnostic()`/`compare_candidates()`
helpers remain available programmatically.

JSON records data/artifact/profile hashes, implementation hashes, runtime versions,
seed, sample sizes, eligible populations, per-user input fingerprints and candidate
counts. Cases use opaque hashes instead of raw user IDs. It also includes the actual
selected movie IDs, so overlap/order metrics can be checked independently. Use the
same pinned dependencies, artifacts, seed, sample size, K and thread limits to
reproduce a run. Source data is read only.

### Baselines, Metrics and Denominators

True Control is obtained separately through the existing service recommendation
functions, with the same history/profile context. Lambda zero is relevance-only
selection through the real reranker on the same expanded pool. The tool verifies
item and order equality instead of assuming it. Catalog validation or candidate
availability can make these baselines differ. If a Control ID is outside the pool,
its percentile is unavailable rather than invented.

Every route is reported separately. Means weight users equally within the named
comparison population; raw SVD/content/popularity scores are never pooled. There is
no overall aggregate. Reported metrics include:

- Mean reference cost per movie and per slate, absolute differences and percentage
  differences versus true Control. Costs use fully priced paired slates only;
  unknown prices are not zero. Percent difference is the difference of paired mean
  slate costs divided by paired mean Control cost, unavailable for zero Control cost.
- Top-K overlap, replaced movie counts/rates and users with replacements, divided
  by fulfilled cases with K valid Control IDs. Replaced-slot rate uses users*K.
- Changed positions, common movies at new positions and actual pairwise inversions
  of common items. These distinguish replacements from relative order changes;
  inversion rates are unavailable when fewer than two common items exist.
- Original mean relevance, mean percentile and their losses versus Control within
  compatible routes. Missing baseline scores/percentiles are unavailable, with
  separate denominators. Lower predicted relevance is not lower observed satisfaction.
- Fulfillment over all attempted users, valid output slots over attempted users*K,
  missing-price rates, catalog membership failures, insufficient pools, other
  failure reasons and no-savings cases among fully priced pairs. Failed cases are
  retained rather than disappearing from the fulfillment denominator.

All JSON numbers are finite or null. The synthetic dataset may have fewer fully
priced pairs than fulfilled users; JSON exposes that denominator for every row.

### Historical Sample Interpretation

For recorded M0, seed 42/sample limit 100/K=20, lambda zero matched true Control in
both items and order for every sampled user. All lambdas fulfilled every case;
there were no missing prices, catalog failures or insufficient pools.

| Lambda | SVD reference cost change | Content reference cost change | Popularity reference cost change |
| --- | ---: | ---: | ---: |
| 0.00 | 0.00% | 0.00% | 0.00% |
| 0.05 | -1.14% | -1.91% | -6.22% |
| 0.10 | -2.47% | -4.66% | -6.22% |
| 0.15 | -3.93% | -6.92% | -6.22% |
| 0.20 | -5.35% | -8.73% | -6.22% |
| 0.30 | -8.65% | -14.83% | -8.06% |
| 0.50 | -17.04% | -26.26% | -14.82% |
| 0.75 | -27.71% | -43.03% | -21.94% |
| 1.00 | -35.31% | -49.73% | -33.52% |

At 0.15, replaced-slot rates were 3.55%/4.60%/5.00%, mean percentile losses
0.000823/0.001434/0.000505, and mean changed positions 14.40/16.30/13.00 of 20
for SVD/content/popularity. Position changes are substantial despite small slate
mean relevance losses. Larger lambdas lower reference costs further on this sample
but increase replacements and proxy relevance losses; cost minimization alone is
not a justified selection rule.

Retain **0.15 provisionally** as a conservative, team-reviewed experiment candidate;
this evaluation does not establish its production optimality. More representative
popularity users, fresh model/catalog/profile data, held-out position-sensitive
quality evaluation and observed rating/engagement guardrails are required before
approval. No causal satisfaction claim, financial saving, or statistical confidence
is inferred. The default is unchanged. Freeze the approved lambda, pool settings
and model pin in the policy version/configuration before any activation.
