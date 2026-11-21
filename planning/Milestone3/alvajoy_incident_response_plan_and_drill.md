# Incident response plan and drill

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

## Recorded drill

On November 20, the team exercised a deliberately injected failure. The
**Monitoring worker is stale** alert reached the whole team through Discord.
The team joined a Discord call, investigated and rolled a fix forward to the
deployment to resolve the issue. The team identified
[#67](https://github.com/jwatson8/The-Neural-Network/pull/67), merged at
05:26 UTC, as the relevant fix; it changed data-drift processing to use
database-side event counts. The PR merge alone does not establish when the fix
was deployed. The supplied [Discord screenshot](evidence/drill_discord_alert.png)
shows the Grafana alert firing; the latest run was approximately 920 seconds
old. The team identified this [heartbeat status capture](evidence/monitoring_heartbeat_status.png)
as recovery evidence; it shows the check up, but does not establish its time
relative to the drill. The injection method, alert and deployment times, and
verification of recovered health and traffic still need to be recorded.

## Controlled drill protocol

**Goal:** exercise the actual monitoring-worker-stale alert and its Discord
delivery without deliberately stopping the recommendation API. An authorized
operator must schedule and approve the drill with the team first. Stopping the
worker creates a monitoring blind spot; run only when the team can watch
independent course traffic and API health and can immediately restore the
worker. Do not run during a real incident or another sensitive operation.

1. Record the UTC start time, operator, approval, recent healthy worker run,
   API health, current traffic and alert state. Confirm Discord access and that a
   second team member can receive and acknowledge the alert. Agree on a maximum
   25-minute injection window and keep the restore command ready.
2. On the authorized VM, from the deployed repository, stop **only** the worker:

   ```sh
   cd /home/the-neural-network
   docker compose --profile monitoring stop monitoring-worker
   ```

   Record the time. Continue watching independent API and course signals. Abort
   and restore immediately if service health degrades, another incident starts,
   or the agreed time limit is reached.
3. Capture the Grafana stale rule transitioning to firing, including timestamp
   and condition. The 600-second threshold plus five-minute pending period,
   evaluation interval and notification grouping mean delivery is not immediate.
   Capture the Discord notification and the named team member's acknowledgement
   and receipt time. The Healthchecks.io heartbeat is an additional signal, not
   a substitute for documenting the selected Grafana alert.
4. Restore the worker even if no alert arrives:

   ```sh
   docker compose --profile monitoring up -d --no-deps monitoring-worker
   docker compose logs --tail=50 monitoring-worker
   ```

   Verify a new persisted monitoring run, resumed heartbeat, rule recovery,
   successful recommendation response and course traffic. Record timestamps and
   review whether any monitoring buckets are missing. If restoration fails,
   escalate using the incident procedure; do not mark the drill successful.
5. Link a sanitized timeline and screenshots or logs showing injection,
   detection, recipient acknowledgement, recovery action and restored health.
   Remove credentials, webhook URLs and sensitive user data from evidence. A
   test-only notification or the drill protocol alone does not establish that
   the configured alert detected the injected failure and reached a person.
