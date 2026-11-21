# Model version switching

Our Flask API uses an in-process router and model registry to serve multiple
registered model versions from one Gunicorn worker with four threads. Experiment
settings pin Control and Treatment to their chosen versions; other requests use
the current default. Pins stay fixed when the default changes. The licensing
experiment currently uses the same underlying model in both arms, with a different
ranking policy in Treatment; this differs from comparing two retrained models.

```text
Question: how do requests continue while the default model changes?

HTTP :8082 -> Flask handler -> request snapshot -> experiment router
                                                | Control pin -> model A
                                                | Treatment pin -> model B + policy
                                                | otherwise -> default model
artifacts/ -> background loader -> validate + warm -> atomically replace default
              old requests retain their original model until they finish
```

The VM's `artifacts/` directory is mounted read-only at `/inputs` in the API.
Models are external files, not baked into the image. A promotion publishes an
activation manifest identifying the version, checksum, and expected previous
model. The background loader verifies and warms the candidate while the current
model serves requests, then publishes one complete serving snapshot. Each request
keeps its snapshot through inference and logging. Failed loading retains the
current model; failed post-activation verification triggers rollback.

Milestone 1 loaded a single model at startup. The registry and router now allow
model-only updates without rebuilding the image or restarting the API. Code
deployments can still interrupt the single API process. This design keeps
deployment simple but shares memory and a failure point across versions.

Implementation: [registry and loader](https://github.com/jwatson8/The-Neural-Network/blob/8e8de53d5aa60c7d3c959c279f97862fef6043fe/src/service/model_registry.py),
[request handling](https://github.com/jwatson8/The-Neural-Network/blob/8e8de53d5aa60c7d3c959c279f97862fef6043fe/src/service/api.py),
[router](https://github.com/jwatson8/The-Neural-Network/blob/8e8de53d5aa60c7d3c959c279f97862fef6043fe/src/service/experiment_routing.py),
[promotion and rollback](https://github.com/jwatson8/The-Neural-Network/blob/8e8de53d5aa60c7d3c959c279f97862fef6043fe/deployment/daily_model.sh),
[external mount](https://github.com/jwatson8/The-Neural-Network/blob/8e8de53d5aa60c7d3c959c279f97862fef6043fe/compose.yaml).
