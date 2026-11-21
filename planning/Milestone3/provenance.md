# Provenance

Each successful recommendation receives a request UUID. PostgreSQL
`recommendation_serves` stores that UUID, serving time, actual `model_version`,
ranking route, and ordered movie IDs. HTTP headers expose `X-Request-ID`,
`X-Model-Version-ID`, and `X-Model-SHA256`; the application log records the same
identity. The API captures one serving snapshot per request, so a concurrent
switch cannot change which model is credited for the result.

The model registry binds each version label to an immutable SHA-256 checksum.
Artifact bytes live at `artifacts/models/<artifact-sha256>.joblib`; a JSON
registration named with the SHA-256 of the **version label** links those bytes to
training provenance. Reusing a version label for different bytes is rejected.
New training records `git_commit_sha` from the training image's baked
`build-revision.txt`, a Python-source checksum, and library versions. The Git
commit identifies the pipeline and learning algorithm; deploying newer serving
code does not change the training commit.

For daily models, `training_data_snapshot` identifies the frozen PostgreSQL
snapshot at `artifacts/daily/<day>/snapshot.json.gz`, the retained original CSVs
under `artifacts/training-data/<checksum>/`, and their checksums. It also records
the event cutoff, deduplication, metadata policy, and sampling caps. Training uses
the original history plus up to 100,000 selected events from the preceding seven
days and up to 100,000 from the held-out day and attribution tail; the final
refit excludes events at or after the day's end. Thus the manifest identifies a
bounded sample, not every production interaction. The database checksum covers
uncompressed JSON bytes; original-file checksums cover the compressed CSV files.

To investigate a past recommendation, find its UUID in the response or application
log, then run the [trace command](https://github.com/jwatson8/The-Neural-Network/blob/8e8de53d5aa60c7d3c959c279f97862fef6043fe/src/model_updates/trace.py)
on the team's VM:

```sh
cd /home/the-neural-network
docker compose -p the-neural-network exec -T api python -m src.model_updates.trace 63fbf04e-125e-4b4c-be73-017dbba79f6e
```

This resolves the stored version to its registration, verifies the model and
retained training-file checksums, and prints the training commit and input
manifest. Course staff with VM access can inspect the files it names and open
`https://github.com/jwatson8/The-Neural-Network/tree/<git_commit_sha>`.

**Concrete captured request:** at 00:11:21 UTC on November 20, 2026, a request for
`/recommend/1` returned HTTP 200 with:

```text
X-Request-ID: 63fbf04e-125e-4b4c-be73-017dbba79f6e
X-Model-Version-ID: m0-svd-v1
X-Model-SHA256: b677c103c9d64d6bbee56e11fc57df2fa3443d8414da89e7dc0a777ad1bb74a8
```

These captured headers establish the served model identity. Before submission,
attach a full VM trace to complete the code-and-data example. Legacy artifacts
may lack the newer training manifest or Git commit; a later deployment cannot
reconstruct missing historical provenance. Retain registrations, artifacts, and
snapshots together. This traces model training inputs; it does not archive every
historical live profile or catalog value used by surrounding serving logic.

Implementation: [prediction audit](https://github.com/jwatson8/The-Neural-Network/blob/8e8de53d5aa60c7d3c959c279f97862fef6043fe/src/service/api.py),
[version binding](https://github.com/jwatson8/The-Neural-Network/blob/8e8de53d5aa60c7d3c959c279f97862fef6043fe/src/model_updates/promotion.py),
[training identity and retained CSVs](https://github.com/jwatson8/The-Neural-Network/blob/8e8de53d5aa60c7d3c959c279f97862fef6043fe/src/model_updates/provenance.py),
[snapshot selection](https://github.com/jwatson8/The-Neural-Network/blob/8e8de53d5aa60c7d3c959c279f97862fef6043fe/src/model_updates/snapshot.py),
[training manifest](https://github.com/jwatson8/The-Neural-Network/blob/8e8de53d5aa60c7d3c959c279f97862fef6043fe/src/model_updates/daily.py).
