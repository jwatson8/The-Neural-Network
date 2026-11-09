# Offline evaluation

**Data and split.** We evaluate the current 32-factor model on the fixed course dataset using a global chronological holdout, rather than splitting each user's timeline independently. After validation and exact-record deduplication, we discard account-created events and choose the timestamp at the 80th percentile of interaction events. All interactions before **2025-01-31 08:43 UTC** train the model; all at or after it are held out. This gives **43,723 training and 10,931 test events**. Equal timestamps stay together. Settings are fixed without test-based tuning; no separate validation set is used.

**Metric.** Our primary metric is macro-averaged binary **NDCG@20**, supplemented by **Recall@20**. **Data gathered:** the latest held-out rating for each user/movie pair defines relevance, with ratings >=7 positive. **Operationalization:** exclude movies seen in training before ranking the common full catalog; omit users with no remaining positive labels. For each eligible user, sum `relevance/log2(rank+1)` over the top 20 and divide by the ideal discounted sum. Recall is the number of recommended relevant movies divided by all eligible relevant movies. Average each metric across users. A popularity baseline uses the same training data, catalog, exclusions, and labels.

| Cohort | Users | Model NDCG@20 | Model Recall@20 | Popularity NDCG@20 | Popularity Recall@20 |
|---|---:|---:|---:|---:|---:|
| All eligible | 199 | 0.1886 | 0.1753 | 0.1886 | 0.1753 |
| At least 10 trained distinct movies | 1 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| Below the SVD threshold | 198 | 0.1895 | 0.1762 | 0.1895 | 0.1762 |

NDCG rewards relevant movies near the top and matches our ranked-list output better than rating-error metrics. Global time separation prevents future interactions from other users entering training. However, the provided catalog/user metadata is a fixed full snapshot. Without timestamped pre-cutoff LLM profiles, cold users receive the implemented popularity fallback; this experiment does not measure LLM personalization. Nearly all eligible users are cold, explaining the tied baseline and preventing a strong conclusion about SVD quality. Unobserved preferences remain unknown, so sparse, exposure-biased labels limit interpretation.

[Implementation](https://github.com/jwatson8/The-Neural-Network/blob/main/src/model_updates/offline.py) reuses the [daily ranking metrics](https://github.com/jwatson8/The-Neural-Network/blob/main/src/model_updates/evaluation.py). [Complete measured results](offline_results.json) include data/source hashes and settings. Reproduce from the repository root with `python -m src.model_updates.offline`; no live API or database is required.
