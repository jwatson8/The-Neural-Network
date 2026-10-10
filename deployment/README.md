# Docker smoke test for the existing SVD API

This configuration tests the API, trained SVD bundle, and saved LLM profiles.
It does not run the profile worker or consume Kafka. New unknown users receive
fallback recommendations and queued jobs remain pending. This is not a complete
Milestone 1 production deployment. The team still needs live user lookup, LLM
credentials, automatic account handling, and course-traffic evidence.

Run all Compose commands from the repository root. The Python import target is
`src.service.api:app`, matching the current absolute imports in the service code.

## Inputs

Create `artifacts/` and copy your trusted M0 files into it:

- `artifacts/model.joblib` (the full trained SVD/content bundle)
- `artifacts/llm_profiles.json` (the saved 50 cold-start profiles)

They are ignored by Git and mounted read-only. No API credentials are needed for
this smoke test. Do not load model files from untrusted sources.

## Start and test on the VM

```sh
test -s artifacts/model.joblib && test -s artifacts/llm_profiles.json
sudo docker compose -f compose.smoke.yaml config --quiet
sudo docker compose -f compose.smoke.yaml up -d --build --wait --wait-timeout 120
sudo docker compose -f compose.smoke.yaml ps
sudo docker compose -f compose.smoke.yaml exec -T api python deployment/smoke_test.py
curl --fail --max-time 0.6 -w '\nHTTP=%{http_code} total=%{time_total}s\n' http://127.0.0.1:8082/recommend/1
```

Stop if either input-file check fails. The automated smoke test checks actual HTTP
responses for three warm users, two seeded cold users, and one unknown user. It
checks status, content type, format, catalog membership, duplicates, seen-item
filtering, variation among warm users, and each sampled response below 600 ms.
These few calls are not a load test, a cold-start worker test, or proof of course
request success. The automated test runs inside the container; the curl command
also checks the VM's published port.

The default binding is VM-local only (127.0.0.1). For external access, after
coordinating with the team, recreate the container with an explicit binding:

```sh
sudo env BIND_ADDRESS=0.0.0.0 docker compose -f compose.smoke.yaml up -d --wait
```

Keep specifying that binding on subsequent Compose updates if external access is
desired. From a separate Windows PowerShell window:

```powershell
curl.exe --fail --max-time 0.6 -w "\nHTTP=%{http_code} total=%{time_total}s\n" http://YOUR_VM_ADDRESS:8082/recommend/1
```

## Inspect and stop

```sh
sudo docker compose -f compose.smoke.yaml logs --tail=100 api
sudo docker compose -f compose.smoke.yaml stop
```

Profiles persist in a named volume. Do not use `down -v` to restart the service.
The API seeds profiles only when absent; replacing the seed JSON does not replace
existing database entries. The model loads at process startup.

## Validation before VM deployment

The application was checked locally over HTTP with the actual M0 artifact and
profiles: all six sampled user requests passed the smoke checks. Docker image
build, Gunicorn, Linux volume permissions, and VM networking still require the
VM commands above; local application checks do not verify those layers.
