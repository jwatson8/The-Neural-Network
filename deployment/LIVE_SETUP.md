# Enable live profiles and course traffic evidence

Status: prepared and locally tested with simulated external calls. Real user API,
OpenAI access, Kafka connection, and log format/timezone must be verified on the VM.
Do not start these services until the connection details are available.

## Information needed from the team/course

- User API host, path, authentication if any, and one example JSON record with
  field names for likes/dislikes. The assignment says `/user/{user_id}`, singular.
  The worker supports top-level fields or a `user` wrapper and configurable field
  names. It currently has no user-API authentication adapter; add one if required.
- Kafka bootstrap host/port, exact team topic, security/authentication settings,
  and whether consumer group names are restricted.
- One real account-created line and recommendation-result line, plus log timezone.
- Permission from the API-key owner to use their billed key and model.

Never put credentials into chat, Git, or a shared screenshot. This setup uses the
existing OpenAI worker. It cannot run without a valid API key. The API container
does not need that key. Only descriptions are sent for profiling, not user IDs.

## After confirming those details

In the VM repository, update this branch with `git pull --ff-only`. Keep any local
changes. If `.env` does not exist, copy `deployment/live.env.example` to `.env`;
otherwise edit the existing file. Set `chmod 600 .env`, then edit with `nano .env`.
Fill all placeholders, verify field names, security settings and log timezone.

The following uses the same Compose project and profiles volume as the smoke test:

```sh
sudo docker compose --env-file .env -f compose.smoke.yaml -f compose.live.yaml config --quiet
sudo docker compose --env-file .env -f compose.smoke.yaml -f compose.live.yaml up -d --build --wait --wait-timeout 120
sudo docker compose --env-file .env -f compose.smoke.yaml -f compose.live.yaml logs --tail=60 profile-worker course-stream
```

Compose reports running state for workers, not successful external connections.
Look for `course_stream_started`, `course_events`, and `profile_created` logs.
Kafka starts at the end for a NEW group and resumes committed offsets thereafter;
it does not recover the previous day's evidence automatically. Keep the same group
and volume. Failed processing must be fixed before its offset can be committed.

## Verify a real new user

Choose a real newly created user with descriptions and no saved profile. Observe
their account-created event, `profile_created user_id=...`, then call
`/recommend/USER_ID`. API logs must show `route=llm_content`. No LLM call runs in the
HTTP handler. Unknown recommendation requests also enqueue jobs as a backup.
If both description strings are empty, the user intentionally keeps popularity
recommendations and is not counted as personalized. An incorrect API schema causes
retries rather than silently treating every user as having no preferences.

Restart the profile worker and verify the same profile is still used. Persisted
SQLite state survives ordinary container restarts. This does not update SVD history
or retrain the model; those are separate later tasks.

## Inspect course evidence

List exact server labels seen in the course stream:

```sh
sudo docker compose --env-file .env -f compose.smoke.yaml -f compose.live.yaml exec -T course-stream python deployment/traffic_report.py --list-servers
```

Then use YOUR exact label and the real submission window (timestamps with offset):

```sh
sudo docker compose --env-file .env -f compose.smoke.yaml -f compose.live.yaml exec -T course-stream python deployment/traffic_report.py --server 'EXACT_TEAM_SERVER_LABEL' --start 'ACTUAL_WINDOW_START_WITH_TIMEZONE'
```

Start should be either 24 hours before submission or the submission time for the
following 24 hours. The default end is 24 hours after start. A report while this
window is still in progress is preliminary. Omitting start gives a rolling 24h
diagnostic window, which is not necessarily the assignment's submission window.

The report counts ALL target-server recommendation requests as denominator,
including failures. Course status 200 is authoritative for success. Personalized
successes must also match one API audit record by user ID, exact returned list,
and timestamp within 120 seconds, with collaborative/content route. Unmatched
records are not counted as personalized. This requires synchronized clocks and
correct log timezone. Inspect unparsed records and consumer lag before relying on
counts. Do not claim a full window if collection started late or records were lost.

Targets: 2,000 successful requests and personalized successes >=10% of ALL requests
in the applicable window. A running container or `profile_created` alone does not
prove those targets. Captured evidence and audit rows persist in SQLite; monitor
disk space during this milestone. Set retention/archiving in later milestones.

## Local validation

`python -m unittest discover -s src/tests -v` tests the API, account-event queueing,
profile persistence, failed-lookup retry, empty-description fallback, Kafka-offset
deduplication, and report denominator/matching. Tests substitute external calls;
they cannot establish that the course endpoints or API key work.

Reference: the worker uses the OpenAI SDK's structured response parsing described
in https://developers.openai.com/api/docs/guides/structured-outputs .
