# Recommendation service

Run from the repository root. The serving entry point is `src.service.api:app`.
See [the deployment guide](../../deployment/README.md) for the VM instructions.

```sh
python -m pip install -r src/requirements-service.txt
python -m src.model.prepare
python -m flask --app src.service.api run --host 127.0.0.1 --port 8082
```

Production uses Gunicorn in Docker. The API loads `artifacts/model.joblib` once,
returns plain comma-separated catalog IDs, and uses SQLite for saved LLM profiles
and pending jobs. SVD is used only when the loaded model includes interactions
with at least **10 distinct movies** for that user. Below that threshold, saved
LLM profiles use the trained TF-IDF features; users awaiting profiles receive
popularity recommendations. Empty profiles also use popularity. All paths exclude
movies already seen in the trained history.

The shared rule is in [`src/model/routing.py`](../model/routing.py), and the API,
command-line recommender, and daily offline evaluation use it. Repeated video
minutes, repeat ratings, and watching then rating the same movie count as one
movie. Low ratings count as interactions too. Account creation does not count.
New interactions affect routing only after retraining and promotion load a new
bundle; there is no live database-count shortcut to stale SVD factors. Ten is a
starting history-size heuristic, not a measured guarantee of user-level accuracy.
Popularity remains the ranking in the deployed artifact and refreshes when that
artifact is replaced; it is not a continuously updated trending list. Existing
model bundles work without migration because they already contain distinct seen
movie sets. Deploying this code applies the rule to those bundles immediately.

The live Compose profile adds `src.service.profile_worker` and
`src.service.course_stream`. Kafka account events and unknown recommendation
requests enqueue profile jobs. User API and LLM calls run only in the worker.
The worker validates preference fields and structured LLM output, persists results,
and retries failures. No service activation is implied until real connections and
credentials are configured. Empty descriptions deliberately use popularity.

Alvajoy's Flask application factory, route split and SQLite schema are retained.
The request path no longer reloads the movie catalog and transforms every movie
on each cold request. Raw descriptions no longer bypass the LLM or prevent queueing.
Optional legacy SQL dataset import remains available with `SEED_DATASETS=1` and
`DATASET_DIR`; it is not required for serving the model catalog.

Tests: `python -m unittest discover -s src/tests -v`.
