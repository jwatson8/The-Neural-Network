
## Experimentation Infrastructure

We deployed a **user-level randomized A/B experiment** in the production recommendation API to evaluate the impact of cost-aware reranking on licensing costs and user satisfaction. **5% of eligible users** are enrolled, split equally between **Control** (existing ranking) and **Treatment** (cost-aware reranking, **λ = 0.15**). Deterministic hashing and PostgreSQL ensure sticky assignments. Both groups use the pinned `m0-svd-v1` model bundle, isolating the ranking policy as the experimental difference. Treatment supports **SVD, LLM-content, and popularity-fallback** routes.

**Measurement and analysis.** Each fulfilled experimental request logs its assignment, route, model version, and request ID. The primary outcome is **actual licensing cost per assigned user** (intention-to-treat), while **movie ratings and rating participation** measure satisfaction. The reporting pipeline uses **Welch's t-test and confidence intervals** to compare group means, with **bootstrap confidence intervals** to account for skewed costs. It also checks ingestion and billing completeness before estimating treatment effects.

**Production verification.** The API returned HTTP 200 with experiment models ready. Repeated requests confirmed **sticky assignments and 4/4 assignment-linked serves**. All three treatment routes were verified, including an SVD request returning 20 recommendations through `cost_aware:collaborative_svd`. The reporting CLI also completed a successful dry run.

**Implementation:** [Experiment assignment](../../src/database/interactions/experiment_assignments.py) · [Recommendation serving and exposure logging](../../src/service/api.py) · [Statistical reporting](../../src/experimentation/reporting.py) · [Reporting CLI](../../src/experimentation/report_cli.py)

## Experiment Results

The experiment began on **November 20, 2026**. An initial reporting snapshot (03:58:47–04:05:54 UTC) recorded **29 Control and 35 Treatment users**, with zero known licensing charges and zero observed raters in both groups. The reporting pipeline generated a preliminary summary; complete cost and rating effects could not yet be estimated.

![Production A/B experiment reporting results](../Milestone3/evidence/experiment_result.png)

*Figure: Initial production A/B experiment reporting snapshot (November 20, 2026 UTC).*
