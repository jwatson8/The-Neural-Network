# Model version switching

Our Flask API uses an in-process router and model registry to serve multiple
registered model versions from one Gunicorn worker with four threads. Experiment
settings pin Control and Treatment to their chosen versions; other requests use
the current default. Pins stay fixed when the default changes. The licensing
experiment currently uses the same underlying model in both arms, with a different
ranking policy in Treatment; this differs from comparing two retrained models.

```text
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

# Regular model updates

An [hourly GitHub Actions trigger](https://github.com/jwatson8/The-Neural-Network/blob/8e8de53d5aa60c7d3c959c279f97862fef6043fe/.github/workflows/daily-model.yml)
uses persisted VM state to enforce 24 hours between attempts. Each attempt
evaluates the previous Eastern calendar day, combining the original dataset with
a bounded sample of recent interactions. A candidate is compared with the serving
model on held-out ratings. Passing the accuracy, latency, and drift gates permits
a final refit and automatic activation without restarting the API.
Implementation: [scheduler](https://github.com/jwatson8/The-Neural-Network/blob/8e8de53d5aa60c7d3c959c279f97862fef6043fe/deployment/scheduled_update.py),
[training](https://github.com/jwatson8/The-Neural-Network/blob/8e8de53d5aa60c7d3c959c279f97862fef6043fe/src/model_updates/daily.py),
[promotion policy](https://github.com/jwatson8/The-Neural-Network/blob/8e8de53d5aa60c7d3c959c279f97862fef6043fe/src/model_updates/policy.py),
[continuity probes](https://github.com/jwatson8/The-Neural-Network/blob/8e8de53d5aa60c7d3c959c279f97862fef6043fe/deployment/observe_update.py).

**Production evidence to finalize:** We will attach two completed promotions within the
72 hours before submission, with version changes, timestamps, continuity reports,
and course-log availability. The [November 20 attempt](https://github.com/jwatson8/The-Neural-Network/actions/runs/44693525057)
retained the original model because of insufficient data; its 29/29 successful
local probes do not demonstrate a model switch.
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

These captured headers establish the served model identity.

Implementation: [prediction audit](https://github.com/jwatson8/The-Neural-Network/blob/8e8de53d5aa60c7d3c959c279f97862fef6043fe/src/service/api.py),
[version binding](https://github.com/jwatson8/The-Neural-Network/blob/8e8de53d5aa60c7d3c959c279f97862fef6043fe/src/model_updates/promotion.py),
[training identity and retained CSVs](https://github.com/jwatson8/The-Neural-Network/blob/8e8de53d5aa60c7d3c959c279f97862fef6043fe/src/model_updates/provenance.py),
[snapshot selection](https://github.com/jwatson8/The-Neural-Network/blob/8e8de53d5aa60c7d3c959c279f97862fef6043fe/src/model_updates/snapshot.py),
[training manifest](https://github.com/jwatson8/The-Neural-Network/blob/8e8de53d5aa60c7d3c959c279f97862fef6043fe/src/model_updates/daily.py).
# Monitoring
## Infrastructure
As of Milestone 3 our project's monitoring infrastructure is robust. Our monitoring infrastructure can be found under [monitoring](../../src/monitoring/). 

The monitoring infrastructure draws from a [PostgreSQL table as "MonitoringRun"](../../src/database/models.py) with a dedicated suite of [functions](../../src/database/interactions/monitoring_runs.py) that allow database interactions. Our [monitoring worker](../../src/monitoring/monitoring_worker.py) runs on *5* minute intervals and computes a number of [calculations](../../src/monitoring/calculations/). Those calculations  the other database tables and logs entries to `MonitoringRun`, and makes an external ping to [HealthCheck.io](../../src/monitoring/healthchecks.py). That table is then read by [Grafana](../../src/monitoring/grafana/), to populate several [dashboards](../../src/monitoring/grafana/dashboards/). 

Finally, the monitoring suite (i.e., the monitoring worker and grafana) are both [containerized](../../compose.yaml).

## What We Monitor
Our dashboard tracks a number of metrics including:
### Data Drift
We measure changes in rating distributions and Kafka event-type distributions, measured using total variation distance (TVD).

![Image of data drift dashboards](../Milestone3/evidence/data_drift_dashboard.png)

## Model Accuracy
We measure model quality as the share of attributed user ratings that are positive (over 6), broken down by recommendation route and model version over a rolling 24-hour window.

![Image of tracking model accuracy dashboards](../Milestone3/evidence/model_accuracy_dashboard.png)

## Licensing and LLM Costs
We track movie licensing charges and estimated LLM inference costs, including requests and token usage.

![Image of dashboard showing licensing and LLM costs](../Milestone3/evidence/live_costs_dashboard.png)

## Latency, Traffic, and Service Availability
We track HTTP 200 success rates, failed/unanswered requests, request traffic, p95 latency, and the fraction exceeding 600 ms.

![Image of successful monitoring pings to HealthCheck.io](../Milestone3/evidence/health_check_service.png)
![Image of latency calculations](../Milestone3/evidence/request_latency_dashboard.png)
![Dashboard showing request traffic](../Milestone3/evidence/request_traffic_dashboard.png)
![Image of availability calculations dashboard](../Milestone3/evidence/availability_dashboard.png)

## Alerts
Our alert system uses Discord integration and a Webhook to send a Ping to all team members in our dedicated channel. This alert is actionable because it labels the triggering-condition. The following tables list the alert triggering conditions. Upon arrival, we initiate our response plan.
## Service Health
These can be found as [operational rules](../../src/monitoring/grafana/provisioning/alerting/operational-rules.yaml)
| Alert | Trigger Condition |
|-------|-------------------|
| Recommendation availability is low | 3 consecutive 5-min buckets has under 70% of requests returning status 200 |
| Recommendation traffic is stopped | 0 successful requests across 3 5-min buckets |
| Monitoring worker is stale | No monitoring worker run is saved for 600s. Also fires if no run exists at all|
| Monitoring Workload Failure to Record| 2 or more monitoring-run exceptions logged in last 600s|

## Latency 
These can be found in [latency rules](../../src/monitoring/grafana/provisioning/alerting/latency-rules.yaml)
| Alert | Trigger Condition |
|-------|-------------------|
| Request p95 latency is high | The latest bucket has 100 or more requests and p95 latency over 600ms |
| Slow request share is high | THe latest bucket has 100 or more requests and over 5% of them take more than 600ms|

## Quality and Cost
These can be found in [quality cost rules](../../src/monitoring/grafana/provisioning/alerting/quality-cost-rules.yaml)
| Alert | Trigger Condition |
|-------|-------------------|
| Data drift detected| Rating or event distribution shift (TVD) reaches .2 or more in the latest bucket|
| Model accuracy low| For any route, the rolling 24-hour rating-rate is under 60% with 100 or more ratings|
| Licensing cost exceeds budget | Rolling 24-hour licensing cost goes over $125,000. This was decided based on historic averages|
## External Dead Man Switch
Monitoring worker pings HealthCheck.io. The service alerts our discord if it doesn't receive a ping every 5 mins (with a 10 min grace period)

# Experimentation Infrastructure

We deployed a **user-level randomized A/B experiment** in the production recommendation API to evaluate the impact of cost-aware reranking on licensing costs and user satisfaction. **5% of eligible users** are enrolled, split equally between **Control** (existing ranking) and **Treatment** (cost-aware reranking, **λ = 0.15**). Deterministic hashing and PostgreSQL ensure sticky assignments. Both groups use the pinned `m0-svd-v1` model bundle, isolating the ranking policy as the experimental difference. Treatment supports **SVD, LLM-content, and popularity-fallback** routes.

**Measurement and analysis.** Each fulfilled experimental request logs its assignment, route, model version, and request ID. The primary outcome is **actual licensing cost per assigned user** (intention-to-treat), while **movie ratings and rating participation** measure satisfaction. The reporting pipeline uses **Welch's t-test and confidence intervals** to compare group means, with **bootstrap confidence intervals** to account for skewed costs. It also checks ingestion and billing completeness before estimating treatment effects.

**Production verification.** The API returned HTTP 200 with experiment models ready. Repeated requests confirmed **sticky assignments and 4/4 assignment-linked serves**. All three treatment routes were verified, including an SVD request returning 20 recommendations through `cost_aware:collaborative_svd`. The reporting CLI also completed a successful dry run.

**Implementation:** [Experiment assignment](../../src/database/interactions/experiment_assignments.py) · [Recommendation serving and exposure logging](../../src/service/api.py) · [Statistical reporting](../../src/experimentation/reporting.py) · [Reporting CLI](../../src/experimentation/report_cli.py)

## Experiment Results

The experiment began on **November 20, 2026**. An initial reporting snapshot (03:58:47–04:05:54 UTC) recorded **29 Control and 35 Treatment users**, with zero known licensing charges and zero observed raters in both groups. The reporting pipeline generated a preliminary summary; complete cost and rating effects could not yet be estimated.

![Production A/B experiment reporting results](../Milestone3/evidence/experiment_result.png)

*Figure: Initial production A/B experiment reporting snapshot (November 20, 2026 UTC).*
# Incident Response Plan & Drill

## Overview & Objectives

This document details the standard operating procedure for incident detection, escalation, communication, and rollback for the Neural Network Recommendation Service, alongside the protocol for executing a live incident drill.

## Incident Response Plan


## Detect and assess

The monitoring worker writes five-minute summaries to PostgreSQL. Grafana reads
them and evaluates the [operational](../../src/monitoring/grafana/provisioning/alerting/operational-rules.yaml),
[latency](../../src/monitoring/grafana/provisioning/alerting/latency-rules.yaml),
and [quality and cost](../../src/monitoring/grafana/provisioning/alerting/quality-cost-rules.yaml)
rules. Grafana sends notifications to the team Discord alert channel through
its [contact point](../../src/monitoring/grafana/provisioning/alerting/notifications.yaml).
Healthchecks.io separately watches the worker heartbeat.

The **Recommendation traffic stopped** rule fires when all three latest
complete five-minute buckets have zero successful requests. The **Monitoring
worker is stale** rule fires after the latest saved run is more than 600 seconds
old and the condition persists for five minutes; missing data or query errors
are configured to alert as well. A stopped worker can make traffic-based alerts
stale, so check the worker and the external heartbeat as well as API health.
The [dashboard access instructions](../../src/monitoring/grafana/README.md)
explain how to connect to Grafana.

On any alert, acknowledge it in the team Discord channel, record the UTC time,
and check whether the signal reflects an API outage, missing ingestion, a
monitoring failure or another fault. Check `/health`, a recommendation response,
course traffic and monitoring freshness. Do not equate missing telemetry with
healthy service.

## Urgent contact and responsibilities

Management or the mentor should send an urgent message to the team in Microsoft
Teams and initiate a Teams call. If no one responds, call a team member using the
phone number pinned in the team's Teams chat. The incident lead confirms receipt
and opens a Discord incident thread; escalate an unresolved severe outage to
course staff through the established course channels after 30 minutes. Contact
details and credentials stay in private team channels, not this public document.

| Role | Responsibility |
|---|---|
| Alvajoy, incident lead | Coordinate response, decide on recovery, record timeline and update stakeholders. |
| Nathan, monitoring lead | Inspect Grafana, course logs and heartbeat; verify alert delivery and recovery. |
| Jace, deployment lead | Inspect service and deployment state; carry out approved recovery or rollback. |
| Frank, data and experiment lead | Check ingestion, data quality, licensing and experiment impact. |

If a named lead is unavailable, the incident lead assigns an available team
member as backup. Post the incident, owner, impact, current action and next
update in the Discord thread; update every 15 minutes. Inform management when
impact, mitigation or expected recovery changes.

## Recovery

Diagnose before changing production. For a bad model promotion, use the
documented date-specific [rollback procedure](../../deployment/DAILY_MODELS.md#approve-promote-and-recover)
and verify activation acknowledgement, the actual model digest and a valid
recommendation. For a code or container failure, follow the
[deployment recovery procedure](../../deployment/CI_CD.md#deploy-a-release);
do not remove volumes or flush queues as a generic recovery step. If monitoring
is down, restore its worker and verify fresh runs and heartbeat delivery.
After mitigation, confirm valid recommendation responses, course traffic,
latency and monitoring freshness, then document impact, root cause, actions,
recovery time and follow-up owners.

# Team Contract Update
 No update to team contract. Our existing team contract remains in effect; see the Milestone 2 report.