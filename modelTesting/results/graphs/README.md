# Model comparison graphs

- Inference source: Evaluation 2026-10-09T04:16:51+00:00
- Training source: Benchmark 2026-10-09T02:39:01+00:00
- Throughput source: Prediction throughput benchmark 2026-10-09T03:21:10+00:00
- Charts use a zero baseline and consistent model colors.

## Pointwise warm-rating quality

### MAE

![MAE](pointwise_mae.png)

### RMSE

![RMSE](pointwise_rmse.png)

### Precision

![Precision](pointwise_precision.png)

### Recall

![Recall](pointwise_recall.png)

### False-positive rate

![False-positive rate](pointwise_fpr.png)

### False-negative rate

![False-negative rate](pointwise_fnr.png)

### Confusion counts

![Confusion counts](pointwise_confusion_counts.png)

## Native top-20 recommendation quality

### Precision@20

![Precision@20](native_precision_at_20.png)

### Recall@20

![Recall@20](native_recall_at_20.png)

### Hit Rate@20

![Hit Rate@20](native_hit_rate_at_20.png)

### NDCG@20

![NDCG@20](native_ndcg_at_20.png)

## Operational characteristics

### Average training time

![Average training time](average_training_time.png)

### Average top-20 request time

![Average top-20 request time](average_top20_request_time.png)

### Model artifact size

![Model artifact size](model_artifact_size.png)

The average-rating baseline is omitted from operational and size charts because it is computed in memory and has no persisted model artifact.

Artifact size measures serialized files on disk, not peak runtime memory.
