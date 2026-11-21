# Incident Response Plan & Drill

## Overview & Objectives

This document details the standard operating procedure for incident detection, escalation, communication, and rollback for the Neural Network Recommendation Service, alongside the protocol for executing a live incident drill.

## Incident Response Plan

### 2.1. Incident Detection

**Monitoring Stack:** Real-time metrics are tracked via Prometheus and Grafana dashboards.

**Core Metrics Monitored:**
- Service Availability & Throughput (tracked via Kafka logs).
- Online Model Prediction Accuracy & Latency.
- Operating Costs (LLM API token costs & licensing fees).
- Data & Concept Drift.

**Automated Alerting:** Automated alert rules are configured to trigger whenever availability drops below threshold, error rates exceed set limits, or data drift occurs. Notifications are automatically routed to Teams Group Chats.

### 2.2 Urgent Mentor & Escalation Contact

**Primary Mentor / TA Contact:** Reach out directly via official course communication channels email, piazza, canvas for severe production-impacting incidents that cannot be resolved within 30 minutes.

**Escalation Trigger:** Unresolved outage exceeding 30 minutes, persistent database corruption, or system failure during the mandatory 72-hour continuous availability window

### 2.3 Team Roles & Responsibilities

- **Infrastructure & Deployment Lead (Jace Watson):** Manages CI/CD pipelines, Docker container runtime, load balancer/router switches, and zero-downtime model version rollbacks.
- **Monitoring & Alerts Lead (Nathan Rothenbaum):** Oversees Grafana/Prometheus dashboard health, inspects Kafka logs, and verifies alert delivery across communication channels.
- **Data & Experimentation Lead (Meng-Ju Tsai / Frank):** Assesses A/B testing impact, monitors licensing cost model performance, and evaluates data quality/drift metrics during incidents.
- **Incident Lead (Alvajoy Asante):** Coordinates overall incident response, makes final decisions on rollback, and maintains stakeholder updates.

### 2.4 Incident Communication Protocol

**Internal Channel:** Emergency voice/text channel setup on team Discord/Slack for active troubleshooting.

**Status Updates:** Update incident status log every 15 minutes during active triage detailing current findings, actions taken, and next steps.

### 2.5 Rollback & Recovery Procedures

- **Model Switch Rollback:** Utilize the lightweight load balancer / router to instantly switch traffic from the faulty model version to the previous stable snapshot mapping (git_commit_sha).
- **Container Rollback:** Revert production deployments to the previous verified stable Docker container image stored in the repository registry.
- **Queue & Worker Clearing:** Flush or scale worker instances (e.g., cold-start backlog workers) to ensure queued requests process normally post-recovery.
- **Post-Recovery Verification:** Perform health checks on the recommendation endpoint to verify 200 OK status codes, normal latency, and valid prediction outputs.

## 3. Live Incident Drill Protocol

### 3.1 Drill Objectives & Preparation

Simulate a production fault to validate alerting pipelines, test response coordination, execute recovery, and document evidence for the Milestone 3 final report.

### 3.2 Step-by-Step Drill Execution

- **Fault Injection:** Controlled disruption (e.g., simulating service failure or injecting invalid requests to trigger error threshold).
- **Alert Verification:** Confirm that Prometheus detects the anomaly and automated alerts trigger external notifications to team members outside the dashboard.
- **Response Execution:** Triage the incident, execute the rollback procedure via the model router/load balancer, and restore operational health.
- **System Recovery:** Validate that service availability, latency, and recommendation metrics return to optimal baseline levels.

### 3.3 Evidence & Artifact Logging Checklist

- Capture screenshots of the Prometheus / Grafana alert firing state.
- Save alert delivery notifications received via Discord.
- Export system and application logs covering the failure, triage, and recovery window.
- Document key timestamps (Fault Injected, Alert Triggered, Triage Initiated, Rollback Executed, Recovery Confirmed) for inclusion in m3_report.pdfh / m3_report.md.
