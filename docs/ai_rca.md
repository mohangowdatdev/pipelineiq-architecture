# PipelineIQ — AI Root Cause Analysis Engine

*Populated at end of Phase 4 and Phase 5.*

---

## Summary

This document covers the full AI RCA loop: how PipelineIQ takes a structured
failure event, retrieves semantically relevant IaC chunks from pgvector, constructs
a context-rich prompt, calls Azure OpenAI GPT-4o, and produces a structured incident
record with a plain-English root cause summary, evidence, and suggested fix.

It will be written once Phase 4 (IaC ingestion and pgvector retrieval) and Phase 5
(FastAPI RCA loop and Slack integration) are both complete and verified against all
6 failure scenarios.

---

## Contents (to be written at end of Phase 4/5)

- IaC ingestion pipeline: webhook from Azure DevOps, chunking strategy, embedding
- pgvector setup: ivfflat index, cosine similarity search, retrieval K
- How IaC chunks are selected for each failure type (what makes a chunk relevant)
- Prompt structure: system prompt, failure event JSON, IaC evidence blocks
- GPT-4o response schema: root_cause_summary, confidence, suggested_fix, evidence
- How confidence is scored
- incident_store schema and the append-only constraint
- Slack webhook payload format
- How the human approval gate works (the /v1/pipelines/{id}/approve-fix route)
- Latency target: Slack alert within 5 minutes of failure detection
- RCA accuracy against each of the 6 failure scenarios
