# react/

PipelineIQ React dashboard frontend.

## Phase

Phase 6 — Dashboard

## Responsibility

Displays real-time pipeline health, incident history, and AI root cause
analysis evidence to the operations team.

## Key panels

1. **Pipeline status board** — all 10 entities, last run time, status, row counts
2. **Incident panel** — AI-generated summaries, confidence ratings, IaC evidence
3. **Fix workflow panel** — human approval gate for AI suggested fixes
4. **Volume trend chart** — 7-day rolling average vs today's counts (catches volume anomaly failure scenario)

## Data source

All data read from FastAPI backend at `/v1/` routes. No direct DB access from frontend.

## Deployment

Azure Static Web Apps (free tier). Build output is the `build/` directory.

## Populated

End of Phase 6.
