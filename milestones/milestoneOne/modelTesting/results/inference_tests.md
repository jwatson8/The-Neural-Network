# Model inference tests

Warm-item rating prediction and recommendation-list evaluation.


## Evaluation 2026-10-09T03:51:23+00:00

- Data directory: `/Users/cheepsahoy/Coding_Projects/certificate/milestoneOne-modelComparisons/data`
- Model directory: `/Users/cheepsahoy/Coding_Projects/certificate/milestoneOne-modelComparisons/models`
- Shared candidate movies: 2023
- Warm held-out ratings: 4893
- Cold held-out ratings excluded: 668
- Positive ratings (`>= 7.5`): 2712
- Negative ratings (`< 7.5`): 2181
- Recommendation list size: 20
- Individual predictions are processed in memory and are not written

### Rating conversions

- Nathan: native estimate clipped to `[1, 10]`.
- Alvajoy: native Surprise SVD estimate.
- Frank: `5.5 + centered similarity sum / absolute similarity sum`, clipped to `[1, 10]`; zero similarity returns `5.5`.
- Jace: inverse training transform `5.5 + 4.5 × raw score`, clipped to `[1, 10]`.
- Average rating baseline: training-only mean rating for the movie.

### Pointwise warm-rating metrics

| Model | Ratings | MAE | RMSE | Precision | Recall | FPR | FNR | TP | FP | TN | FN |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Nathan | 4893 | 1.406834 | 1.810024 | 0.768605 | 0.453171 | 0.169647 | 0.546829 | 1229 | 370 | 1811 | 1483 |
| Alvajoy | 4893 | 1.354104 | 1.745548 | 0.748283 | 0.562316 | 0.235213 | 0.437684 | 1525 | 513 | 1668 | 1187 |
| Frank | 4893 | 1.478946 | 1.944320 | 0.701351 | 0.574115 | 0.303989 | 0.425885 | 1557 | 663 | 1518 | 1155 |
| Jace | 4893 | 2.186539 | 2.585048 | 1.000000 | 0.001106 | 0.000000 | 0.998894 | 3 | 0 | 2181 | 2709 |
| Average rating baseline | 4893 | 1.526494 | 2.019417 | 0.673134 | 0.588496 | 0.355342 | 0.411504 | 1596 | 775 | 1406 | 1116 |

### Native top-20 recommendation metrics

Only users with at least one positive warm held-out rating are included. Unobserved catalog movies are treated as non-relevant, making precision conservative.

| Model | Users | Precision@20 | Recall@20 | Hits | Relevant |
| --- | ---: | ---: | ---: | ---: | ---: |
| Nathan | 894 | 0.016443 | 0.091704 | 294 | 2712 |
| Alvajoy | 894 | 0.011689 | 0.069575 | 209 | 2712 |
| Frank | 894 | 0.021868 | 0.140977 | 391 | 2712 |
| Jace | 894 | 0.025839 | 0.160291 | 462 | 2712 |
| Average rating baseline | 894 | 0.000168 | 0.001156 | 3 | 2712 |


## Evaluation 2026-10-09T04:16:51+00:00

- Data directory: `/Users/cheepsahoy/Coding_Projects/certificate/milestoneOne-modelComparisons/data`
- Model directory: `/Users/cheepsahoy/Coding_Projects/certificate/milestoneOne-modelComparisons/models`
- Shared candidate movies: 2023
- Warm held-out ratings: 4893
- Cold held-out ratings excluded: 668
- Positive ratings (`>= 7.5`): 2712
- Negative ratings (`< 7.5`): 2181
- Recommendation list size: 20
- Individual predictions are processed in memory and are not written

### Rating conversions

- Nathan: native estimate clipped to `[1, 10]`.
- Alvajoy: native Surprise SVD estimate.
- Frank: `5.5 + centered similarity sum / absolute similarity sum`, clipped to `[1, 10]`; zero similarity returns `5.5`.
- Jace: inverse training transform `5.5 + 4.5 × raw score`, clipped to `[1, 10]`.
- Average rating baseline: training-only mean rating for the movie.

### Pointwise warm-rating metrics

| Model | Ratings | MAE | RMSE | Precision | Recall | FPR | FNR | TP | FP | TN | FN |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Nathan | 4893 | 1.406834 | 1.810024 | 0.768605 | 0.453171 | 0.169647 | 0.546829 | 1229 | 370 | 1811 | 1483 |
| Alvajoy | 4893 | 1.354104 | 1.745548 | 0.748283 | 0.562316 | 0.235213 | 0.437684 | 1525 | 513 | 1668 | 1187 |
| Frank | 4893 | 1.478946 | 1.944320 | 0.701351 | 0.574115 | 0.303989 | 0.425885 | 1557 | 663 | 1518 | 1155 |
| Jace | 4893 | 2.186539 | 2.585048 | 1.000000 | 0.001106 | 0.000000 | 0.998894 | 3 | 0 | 2181 | 2709 |
| Average rating baseline | 4893 | 1.526494 | 2.019417 | 0.673134 | 0.588496 | 0.355342 | 0.411504 | 1596 | 775 | 1406 | 1116 |

### Native top-20 recommendation metrics

Only users with at least one positive warm held-out rating are included. Unobserved catalog movies are treated as non-relevant, making precision conservative.

| Model | Users | Precision@20 | Recall@20 | Hit Rate@20 | NDCG@20 | Hits | Relevant |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Nathan | 894 | 0.016443 | 0.091704 | 0.248322 | 0.053018 | 294 | 2712 |
| Alvajoy | 894 | 0.011689 | 0.069575 | 0.196868 | 0.039805 | 209 | 2712 |
| Frank | 894 | 0.021868 | 0.140977 | 0.296421 | 0.069663 | 391 | 2712 |
| Jace | 894 | 0.025839 | 0.160291 | 0.359060 | 0.085278 | 462 | 2712 |
| Average rating baseline | 894 | 0.000168 | 0.001156 | 0.003356 | 0.000978 | 3 | 2712 |

