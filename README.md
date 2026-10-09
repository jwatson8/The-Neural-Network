# Model comparison training

All four contributor models train from one deterministic, per-user chronological
split. Watch and rating rows for the same user/movie pair always remain together.

## Environment

Install Python 3.12, then create the shared environment:

```bash
bash setup_env.sh
source .venv/bin/activate
```

`.venv` contains Python dependencies. `.env` is reserved for optional runtime
configuration and secrets; copy `.env.example` only when those values are needed.

## Benchmark all models

```bash
python train_all.py
```

By default, the command trains and reloads each of the four models 20 times. It
prints the completed training and load time after every model, along with total
elapsed time and an estimated time remaining. Use `--runs` to change the number
of repetitions:

```bash
python train_all.py --runs 20
```

The command appends all individual measurements and per-model averages to
`results/training_times.md`. Training time includes fitting and saving; load time
includes reloading and validating the saved artifact. At the start of each round,
the four artifacts from the previous round are deleted before that round trains.
The four artifacts produced by the final round remain in `models/`.

## Benchmark prediction throughput

After training leaves the final four artifacts in `models/`, run:

```bash
python throughput.py
```

The command makes 100 top-20 recommendation requests per model. Every request
uses the same candidate catalog: the 2,023 movies represented in training. Model
loading and one warm-up request are excluded from prediction timing. Progress,
elapsed time, and estimated time remaining are printed for every request.

Individual timings, average latency, requests per second, and warm/cold test-item
coverage are appended to `results/training_times.md`. Jace retains its full
catalog model, but the throughput benchmark supplies the shared candidate
allowlist so all four timed workloads are directly comparable.

The warm test cohort contains movies represented in training while preserving
held-out user/movie pairs. Movies first encountered in the test split are
reported as cold items and excluded from this shared throughput benchmark.

## Evaluate inference quality

After training, run the warm-item inference evaluation:

```bash
python inference_tests.py
```

The pointwise evaluation processes every warm held-out rating and reports MAE,
RMSE, precision, recall, false-positive rate, and false-negative rate using a
default positive threshold of 7.5. Frank's centered similarity signal and
Jace's normalized factor score are converted back to documented 1–10 estimates
for this table.

A separate native recommendation evaluation reports Precision@20 and Recall@20
for all four models plus an average-movie-rating baseline. Every contender ranks
the same training-supported catalog, and each user's training-seen movies are
excluded. Only aggregate results are appended to
`results/inference_tests.md`; individual predictions are not written.

## Generate comparison graphs

After the training, throughput, and inference result files have been populated,
generate the PNG graph gallery:

```bash
python generate_graphs.py
```

The command reads the latest complete result sections and writes 14 charts plus
an embedded Markdown gallery to `results/graphs/`. It includes pointwise quality,
native top-20 precision, recall, hit rate, and NDCG, average training time,
average top-20 request time, and serialized model size. It does not rerun
training or inference.

Alvajoy's saved model is the submitted Surprise SVD. The separate TF-IDF example
in `Alvajoy/train.py` is retained as an unsupported legacy demonstration because
it does not use SVD and cannot run against the supplied schema as written.

## Test

```bash
python -m unittest discover -s tests -v
```
