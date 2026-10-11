# Deploy the integrated M0 + Flask service

Run Linux commands below in the VM's SSH session, from the repository root.
Review/merge the integration PR first or check out its branch for a team-approved
test. Keep `.env` private. API-only deployment works without external credentials;
full M1 cold-start operation requires the course services and an authorized LLM key.

## 1. Prepare alongside the running smoke service

Use a separate checkout so the old service remains reproducible during testing:

```sh
cd ~/apps
git clone --branch integration/m0-flask-deployment https://github.com/jwatson8/The-Neural-Network.git The-Neural-Network-integrated
cd The-Neural-Network-integrated
git rev-parse HEAD
mkdir -p artifacts
cp .env.example .env
chmod 600 .env
sudo docker compose build api
sudo env LOCAL_UID="$(id -u)" LOCAL_GID="$(id -g)" docker compose --profile tools run --rm train
```

If using the merged PR, clone `main` instead. If the checkout or `.env` already
exists, reuse it and preserve any existing edits/configuration. This creates a
fresh M0 model using the included, checksum-verified dataset. It does not retrain
from live events. Record the source commit and `artifacts/model.json`.

## 2. Test on an alternate port before switching production

```sh
sudo env PORT=18082 BIND_ADDRESS=127.0.0.1 PROFILE_VOLUME_NAME=neural-network-staging-profiles docker compose -p neural-network-staging up -d --wait --wait-timeout 120
sudo docker compose -p neural-network-staging exec -T api python deployment/smoke_test.py
curl --fail --max-time 0.6 http://127.0.0.1:18082/recommend/1
```

The alternate port and separate database leave the old service intact. The smoke
test checks warm, seeded cold and unknown users, valid catalog IDs, formatting,
seen-item filtering and sampled response latency. Inspect logs if anything fails:
`sudo docker compose -p neural-network-staging logs --tail=80 api`.

## 3. Switch the VM after the staging checks pass

Stop only the known old API container after confirming its name with
`sudo docker ps`. The previous deployment used `neural-network-smoke-api-1`:

```sh
sudo docker stop neural-network-smoke-api-1
sudo docker compose up -d --wait --wait-timeout 120
sudo docker compose exec -T api python deployment/smoke_test.py
curl --fail --max-time 0.6 http://127.0.0.1:8082/recommend/1
sudo docker compose -p neural-network-staging stop
```

Keep the old image, checkout and model for rollback. If other old workers are
running, coordinate stopping them before enabling the new workers to avoid duplicate
consumers/profile generation. The default named volume reuses the smoke deployment's
`neural-network-smoke_smoke-profiles`; schema additions preserve its profiles and jobs.
Do not delete volumes or run old and new APIs on the same port.

From Windows PowerShell outside SSH, verify external reachability:

```powershell
curl.exe --fail --max-time 0.6 http://CMU-ONLINE-MLIP-VM01.ANDREW.CMU.EDU:8082/recommend/1
```

## 4. Activate automatic cold start

Get the course user API URL, response field names, Kafka bootstrap servers, team
topic, authentication details, log timezone and permission to use the LLM key.
Edit `.env` directly on the VM with `nano .env`. Do not paste it into chat or Git.
The assignment's user path is `/user/{user_id}`, singular, but confirm the real API.
The worker accepts a top-level object or `user` wrapper with configurable preference
field names; an optional user API bearer token is supported. Kafka supports
PLAINTEXT/SSL/SASL settings; custom certificates require a mounted CA file and Compose
configuration matching the course instructions. Do not disable TLS verification.

```sh
sudo docker compose --profile live config --quiet
sudo docker compose --profile live up -d --wait --wait-timeout 120
sudo docker compose logs --tail=80 cold-start-worker course-stream
```

Check a real newly created user: Kafka queues the account; `profile_created`
confirms a saved interpretation; a later recommendation must log `route=llm_content`.
Requests return popularity while a profile is pending. Raw descriptions alone do
not bypass the LLM. Empty descriptions keep popularity and do not count as
personalized. Profiles persist through restarts. No retraining/redeployment is
needed for profile updates, but new interactions require a later retraining pipeline
before becoming part of SVD history.

Kafka uses a unique stable consumer group, starts at the latest offset for a NEW
group, and resumes committed offsets afterward. Do not share a group with another
team process: Kafka would split its messages between consumers. Historical traffic
is not automatically recovered. Confirm actual event format against the parser
before relying on collection. Health of the API does not prove worker connectivity.

## 5. Verify the grading evidence

```sh
sudo docker compose --profile live exec -T course-stream python deployment/traffic_report.py --list-servers
sudo docker compose --profile live exec -T course-stream python deployment/traffic_report.py --server 'EXACT_TEAM_SERVER_LABEL' --start 'ACTUAL_WINDOW_START_WITH_TIMEZONE'
```

Choose either the submission time for the following 24h, or 24h before submission
for the preceding window. `--start` must include timezone; `--end` defaults to 24h
later. Counts before the window finishes are preliminary. Verify collection spans
the whole window and the consumer is caught up. `COURSE_LOG_TIMEZONE` is applied
only to timestamps with no offset; confirm it with the course.

The denominator includes ALL target-server recommendation requests, including
failures. Status 200 determines course success. Personalized successes additionally
match API audit records by user, exact movie list and timestamp within 120 seconds;
one audit record can match only once. Unmatched records are not called personalized.
Targets are >=2,000 successes and personalized successes >=10% of all requests.
The course logs remain authoritative. Monitor VM disk usage; SQLite evidence is
retained for M1 and needs an archiving/retention policy for long-term operation.

## Rollback

If the new deployment fails, stop it and restart the previous API:

```sh
sudo docker compose --profile live stop
sudo docker start neural-network-smoke-api-1
curl --fail --max-time 2 http://127.0.0.1:8082/health
```

Rollback restores the previous API but does not provide the new live-worker
features. Never use `down -v` as a restart command. Take a consistent SQLite backup
before future incompatible schema changes; this PR only adds tables/indexes.

## What has been verified

Local tests: checksum-verified training on 54,704 events; 14 tests covering API,
queue/persistence/retry, all 50 cached profiles and three warm-user rankings matching
M0, and a new user receiving personalized results after background processing.
External services are mocked in those tests. GitHub Actions checks Linux Docker;
inspect the PR check result. The existing smoke service was verified on the VM,
but this integrated release still needs the staged VM commands above and real
Kafka/user-API/LLM verification. No deployment is claimed by creating this PR.
