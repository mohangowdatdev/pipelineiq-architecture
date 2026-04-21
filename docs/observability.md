# PipelineIQ — Observability and Failure Detection

*Populated at end of Phase 3.*

---

## Summary

This document covers how PipelineIQ detects pipeline failures and structured
anomalies using Azure Monitor and Log Analytics. It describes the diagnostic
settings on ADF and Databricks, the KQL queries FastAPI uses to poll for
failure events, the structured failure event schema in PostgreSQL, and the
volume anomaly detection logic.

It will be written once Phase 3 observability setup is complete and each of the
6 failure scenarios has been confirmed to produce a structured detection event
in PostgreSQL within 5 minutes.

---

## Contents (to be written at end of Phase 3)

- Azure Monitor diagnostic settings: what ADF and Databricks emit
- Log Analytics workspace setup and data retention
- KQL queries: detecting notebook failures, schema errors, DQ rejections
- Volume anomaly detection: 7-day rolling average comparison
- failure_events table schema in PostgreSQL
- Event severity classification
- Polling interval and FastAPI background task design
- How each of the 6 failure classes produces a detectable event
- Latency target: event detected within 5 minutes of failure
