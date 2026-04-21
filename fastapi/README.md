# fastapi/

PipelineIQ FastAPI backend — the AI observability engine.

## Phase

Phase 5 — AI RCA Backend

## Responsibility

Orchestrates the AI root cause analysis loop:

1. Polls Azure Monitor / Log Analytics via KQL for pipeline failures
2. Structures failure events and writes to PostgreSQL failure_events
3. Retrieves relevant IaC chunks from pgvector (semantic similarity search)
4. Constructs prompt and calls Azure OpenAI GPT-4o for RCA
5. Writes structured incident record to pipelineiq.incident_store
6. Fires Slack webhook with plain-English summary
7. Exposes REST API for React dashboard to read incidents

## Routes (all under /v1/)

| Route | Method | Purpose |
|---|---|---|
| /v1/incidents | GET | List all incidents, paginated |
| /v1/incidents/{id} | GET | Get one incident with full evidence |
| /v1/pipelines/status | GET | Current pipeline run status |
| /v1/pipelines/{id}/approve-fix | POST | Human approval gate for AI suggested fix |
| /v1/iac/ingest | POST | Webhook endpoint called by Azure DevOps on IaC push |
| /v1/health | GET | Liveness check |

## Deployment

Azure Container Apps, scale to zero. Python 3.11. Uvicorn.

## Tests

pytest with mock PostgreSQL and mock OpenAI client.
Never call real Azure services or real OpenAI in unit tests.

## Populated

End of Phase 5.
