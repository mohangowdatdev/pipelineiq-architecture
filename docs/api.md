# PipelineIQ — FastAPI Backend Reference

*Populated at end of Phase 5.*

---

## Summary

This document is the complete API reference for the PipelineIQ FastAPI backend.
It describes every route, its request and response schema, authentication mechanism,
error codes, and example payloads. It also describes the background tasks that drive
the observability loop.

It will be written once Phase 5 FastAPI build is complete and all routes are verified
end-to-end against a real PostgreSQL instance.

---

## Contents (to be written at end of Phase 5)

- Authentication: managed identity, API key strategy
- All /v1/ routes with request/response schemas
- Background tasks: KQL polling, failure event detection
- IaC ingest webhook: payload format, auth, idempotency
- Error handling and status code conventions
- Rate limits and timeout values
- Deployment: Container Apps config, scale-to-zero behaviour
- Environment variables required
- Local development setup
