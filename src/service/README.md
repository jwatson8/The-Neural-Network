# Recommendation API

Install `requirements-service.txt`. By default, the API loads the trained Jace
model from `src/app/fullTrainedModel/jace_model.joblib`; set `MODEL_PATH` only
when deploying a different trusted artifact. Start the API and profile worker
in separate processes:

```powershell
$env:MODEL_VERSION = "jace-svd-v1"
$env:PROFILE_DB_PATH = "state/profiles.sqlite3"
$env:PROFILE_SEED_PATH = "llm_profiles.json"
python -m flask --app service.api run --host 0.0.0.0 --port 8082
```

```powershell
$env:USER_LOOKUP_URL_TEMPLATE = "http://localhost:8080/users/{user_id}"
# Set OPENAI_API_KEY in the shell or deployment environment before starting.
python -m service.profile_worker
```

For a Linux VM, run the API with `gunicorn --bind 0.0.0.0:8082 --workers 1
--threads 4 service.api:app`. Keep the worker as a separate process and configure
both processes with the same `PROFILE_DB_PATH`.

The lookup URL is a provisional contract: it must return JSON containing
`self_description_likes` and `self_description_dislikes` strings, either at the
top level or under `user`. Keep the API base URL and credentials in environment
configuration. The worker calls this endpoint and OpenAI asynchronously; neither
call is made while answering `/recommend/<userid>`.

The API seeds SQLite from `PROFILE_SEED_PATH` when that file has the
`{"profiles": {"user-id": {"positive": "...", "negative": "..."}}}` shape.
Users with interaction history in the trained model receive collaborative SVD
recommendations. Users without interaction history are ranked from their
preference descriptions and the movie catalog queried directly from SQLite,
using the model's TF-IDF vectorizer; popularity breaks ties and is the fallback
when descriptions are empty. An ID with neither a local user row nor a saved
profile is queued once and immediately receives popularity recommendations.
After the worker saves a profile, later requests use content ranking against
the SQLite movie catalog. SQLite uses WAL so the API and worker can share the
same database file.

The account-created Kafka schema and credentials are not available in this
workspace, so Kafka consumption is not included yet. Unknown recommendation
requests cover delayed or missed account events, as required for the fallback
path. Add the course event consumer once its payload and connection contract are
known. The profile worker's user lookup endpoint remains an external deployment
input and must be configured with `USER_LOOKUP_URL_TEMPLATE`.

Run focused checks from the repository root with
`python -m unittest src.tests.test_service`. The endpoint
returns only up to 20 catalog IDs, comma-separated on one line, as `text/plain`.