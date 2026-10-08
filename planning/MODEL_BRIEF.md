# Model facts for the Milestone 1 kickoff

Measured October 8, 2026 on the local Windows machine using Python 3.12.14,
scikit-learn 1.6.1 and one numerical-library thread. The model was not changed.
Source: [benchmark code](benchmark_m1.py), [complete results](model_metrics.json).
Run from the repository root: `.venv\Scripts\python.exe benchmark_m1.py` on Windows,
or `python benchmark_m1.py` in an activated environment. Costs vary by machine/load.

## What to say at the meeting

"My model combines a 32-factor SVD collaborative recommender with LLM-generated
preference profiles and TF–IDF content matching for users without history. It
beats popularity on our exploratory ranking test, but it is not a calibrated
rating predictor. The serving bundle is small and local inference is fast. The
main production gap is automatic profile generation and serving for new live
users; the current cold-start path only supports the fixed offline dataset."

## Measured quality

Ranking results are macro averages over 948 users with at least one test rating
of 7–10 and some training history. We rank the full 2,569-movie catalog, excluding
training-history movies. Unobserved movies contribute zero observed relevance;
that does not establish that the user dislikes them.

| Measure | SVD | Popularity baseline | Meaning |
|---|---:|---:|---|
| Precision@10 | 3.18% | 1.84% | Fraction of ten suggestions known to be positive in the test set |
| Recall@10 | 8.76% | 5.62% | Fraction of held-out positive movies found in ten suggestions |
| Hit rate@10 | 24.47% | 15.61% | Users with at least one known-positive suggestion |
| NDCG@10 | 0.0593 | 0.0383 | Ranking quality, rewarding positives nearer the top |
| Precision@20 | 2.74% | 1.64% | Same precision definition for twenty suggestions |
| Recall@20 | 14.97% | 10.02% | Same recall definition for twenty suggestions |
| Hit rate@20 | 37.03% | 22.68% | Users with at least one known-positive suggestion in twenty |
| NDCG@20 | 0.0826 | 0.0539 | Position-sensitive ranking score at twenty |
| Catalog coverage@20 | 11.37% (292 movies) | 1.48% (38 movies) | Distinct recommended movies / 2,569 |

Precision is low partly because only a few future preferences are recorded per
user. A recommendation absent from that short recorded window counts as a miss,
even if the user might like it. Do not describe precision as overall accuracy.
NDCG uses binary relevance here, not graded rating gains.

### RMSE, MAE, and "accuracy"

The program normally produces ranking scores, not 1–10 ratings. For this diagnostic
only, invert the training scale: `predicted_rating = clip(5.5 + 4.5*score, 1, 10)`.
This conversion was not fitted or tuned on the test data. Evaluate all 5,374
held-out ratings, including negative and neutral ratings.

| Diagnostic | SVD with converted scores | Constant training-mean rating |
|---|---:|---:|
| RMSE (rating points; lower is better) | 2.6021 | 2.0565 |
| MAE (rating points; lower is better) | 2.2079 | 1.6402 |
| Binary accuracy: rating >=7 means positive | 36.38% | 63.90% |

The constant baseline predicts 7.1536 for every movie. It therefore labels every
test example positive, and 63.90% of test ratings are positive. The SVD conversion
labels only 0.316% of test examples positive. This shows poor rating calibration;
SVD reconstruction scores are pulled toward zero (5.5 on the rating scale) by
the many missing matrix entries. The model should not be presented as "accurate"
at predicting star ratings. A calibrated rating model would need training/validation
data and a separate untouched test set. No calibration or hyperparameter search
was performed here.

RMSE squares errors before averaging, so it penalizes large errors more strongly.
MAE is the average absolute error. Plain classification accuracy is not a natural
primary metric for this ranked recommendation task. Use Recall@20 or NDCG@20 as
the team's prediction-quality metric if all models can produce the same candidate
rankings under the same evaluation rules.

## Exactly how the data was split

There are 27,327 user/movie pairs and 54,654 watch/rating events. Fifty separate
account-created events are excluded from the quality split because they contain
no outcome labels.

For each user, order distinct movie pairs by their last event timestamp, breaking
ties by movie ID. Hold out the final `max(1, floor(0.2*n))` pairs. Put both the watch
and rating for a held-out pair in test. This produces:

- Training: **21,953 pairs / 43,906 events (80.33%)**.
- Test: **5,374 pairs / 10,748 events (19.67%)**.
- Ranking relevance: **3,434 positive test movies across 948 users**.
- Rating diagnostics: **all 5,374 test ratings**.

The small deviation from 80/20 comes from rounding separately for each user.
The deployed/offline final artifact is then trained on the entire supplied dataset;
test results come from a separately fitted holdout model, not that full-data artifact.

This is an exploratory per-user split, not a global-time production simulation:
another user's later calendar events can be in training. The original single
global cutoff, January 31, 2025 at 08:43, leaves just one eligible warm user; both
methods scored zero Recall@10 there. Neither result demonstrates live user value.
The 50 cold-start users have no future outcome labels, so we have functionality
checks but no cold-start accuracy estimate.

## Measured costs and size

| Measure | Result | Boundary |
|---|---:|---|
| Full training pipeline wall time | **1.41 seconds** | Median of 3 runs; load/validate + fit + serialize |
| Fitting alone | **0.60 seconds** | Median of 3 runs; includes SVD and TF–IDF |
| Warm-user latency p50 / p95 | **3.56 / 7.08 ms** | In-process, loaded model, K=20 |
| Cached cold-profile latency p50 / p95 | **4.69 / 7.28 ms** | Includes TF–IDF matching; excludes LLM generation |
| Warm / cached-cold serial call rate | **234 / 197 calls/sec** | One local serial caller; not production throughput |
| Complete saved serving bundle | **4.399 MiB** | 4,612,719 bytes, uncompressed joblib |
| Saved 50 LLM profiles | **36,446 bytes** | Separate JSON file |
| SVD factor arrays only | **926,464 bytes** | Subset of the serving bundle |

Each latency route used 50 warmups and 1,000 measured calls, round-robin over sorted
IDs. Model loading, network, HTTP handling, queueing, metadata requests, and LLM
generation are excluded. No RAM measurement, cloud dollar cost, Docker image size,
or live-service SLA result is claimed. An Ollama deployment also needs its LLM
weights, runtime, and generation capacity measured separately.

## The four required measures: repeatable specification

| Quality | 1. Metric | 2. Data gathered | 3. Operationalization |
|---|---|---|---|
| Prediction quality | Macro Recall@20; optionally NDCG@20 | Same course snapshot, held-out positive ratings >=7, top-20 IDs from each model | Use the pair split above. Fit only on training interactions. Rank full supplied catalog minus training-history movies. Per eligible user, compute hits / positive test movies; average users equally. NDCG uses sum(relevance/log2(rank+1)) divided by ideal ordering. |
| Training cost | Median elapsed seconds | Three complete full-data training runs on the same machine/software/thread settings | Pre-download inputs. Start timer before CSV load/validation; stop after uncompressed serving artifact serialization. Include preprocessing and fitting; exclude interpreter launch, download and LLM generation, report those separately if applicable. Do not clear OS cache. |
| Inference cost | p95 elapsed milliseconds at K=20, separate warm and cached-cold paths | 1,000 calls per path after 50 warmups, loaded full-data artifact | One serial caller; sorted IDs in round-robin order; time each complete recommendation function. Include ranking/filtering/content matching, exclude HTTP/network/profile generation. All models must use the same eligible ID lists; explicitly count failures, not silently skip unsupported users. |
| Disk size | Uncompressed serving artifact bytes | Saved model artifact and any separately required profiles | Serialize with joblib compression disabled for this model; count bytes with filesystem stat. For each teammate, include all model/tokenizer/index files actually required, consistently excluding environment and raw training data. Report separate LLM weights if needed. Disk size meets the rubric; RAM need not be invented. |

Every teammate must rerun on the agreed data/hardware/protocol before comparing.
The existing code's comparison is SVD versus popularity, not the required comparison
against every teammate's Milestone 0 model. Collect their repository and commit links.

## Production gaps to raise early

1. No HTTP endpoint, Docker Compose deployment, or VM validation exists yet.
2. `recommend()` requires cold users to be in the fitted users list. Truly new live
   users currently fall back to popularity unless this interface is adapted.
3. Automate account-created ingestion, user metadata retrieval, LLM interpretation,
   and persistent profile caching. A background worker keeps LLM generation out
   of the 600 ms request path; pending users need a fast fallback and retry handling.
4. Only 1,000 users have collaborative history in this snapshot. Do not assume
   that covers 10% of live requests. Measure personalized-success / **all requests**.
5. Verify >=2,000 course-logged status-200 responses in the applicable 24-hour
   grading window. Local benchmarks and self-generated smoke tests are not evidence
   that course requests were successfully processed.

Definitions: [scikit-learn NDCG](https://scikit-learn.org/stable/modules/generated/sklearn.metrics.ndcg_score.html),
[scikit-learn RMSE](https://scikit-learn.org/stable/modules/generated/sklearn.metrics.root_mean_squared_error.html).
