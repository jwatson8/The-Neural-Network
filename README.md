# The Neural Network recommendation service

The selected M0 SVD model is trained by `src/model` and served through Alvajoy's
Flask app in `src/service/api.py`. Docker Compose runs the API and, when configured,
the background LLM worker and course Kafka consumer. Python 3.12 is used throughout.

## Train and run locally

From the repository root, in a Python 3.12 virtual environment:

```sh
python -m pip install -r src/requirements-service.txt
python -m src.model.prepare
python -m flask --app src.service.api run --host 127.0.0.1 --port 8082
```

Training verifies the SHA-256 hashes of the three course CSV files already in
`milestones/milestoneOne/modelTesting/data`, then writes `artifacts/model.joblib`
and an adjacent provenance JSON file. No copy from someone's laptop is required.
Missing data may be downloaded with `python -m src.model.prepare --download`.
The prepared model uses all recorded interactions; holdout metrics remain separate.

`src/model/recommender.py` preserves the original M0 learning/ranking implementation
from [Jace's M0 repository](https://github.com/jwatson8/S3D17645_Movie_Recommendation/tree/b131afb5a05f0e86b6047508a2e9a29a1fbcf2e5),
with paths adapted to this repository. `src/model/llm_profiles.json` contains the
original 50 saved LLM interpretations. New users are supported by the live worker.

The comparison models and results under `milestones/` are unchanged. Older
`src/app/switchBoard.py` and `coldStartApproach.py` are prototypes, not the serving
entry point. The generated artifact above is the deployed source of truth, not the
older checked-in `src/app/fullTrainedModel/jace_model.joblib`.

## Deploy and test

Follow [deployment/README.md](deployment/README.md) for Docker build, training,
migration from the running smoke container, activation of live services, and rollback.
The HTTP contract is `/recommend/<userid>` returning at most 20 comma-separated
catalog IDs as plain text. `/health` succeeds only with a loaded model and readable
profile database.

```sh
python -m unittest discover -s src/tests -v
```

The integration tests train the real dataset, check M0/Flask ranking equivalence,
and verify fallback-to-profile transitions and persistence with mocked external
calls. GitHub Actions additionally builds Docker, trains inside it, and exercises
the actual HTTP endpoint. Neither test suite proves the course endpoints or LLM
key work; verify those on the VM before claiming full M1 deployment.
