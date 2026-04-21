# docs/

Phase-by-phase documentation. Written at the end of each phase, not before.

## Why docs are written after building

Documentation written before building is speculation. It describes what we
intended to build, not what we actually built. When the code and the docs
diverge (they always do), the docs become actively misleading.

The rule here: write the doc at the END of the phase. At that point you know
exactly what was built, what changed from the plan, and what the next phase
needs to know. The doc becomes a stable handoff artefact, not a moving target.

## Contents

| File | Covers | Written |
|---|---|---|
| architecture.md | Full system topology, component roles, data flows | End of Phase 0 |
| data_generation.md | Generator design, volumes, failure scenarios, runbook | End of Phase 1 |
| pipeline.md | ADF pipeline design, watermark strategy, Databricks job config | End of Phase 2 |
| observability.md | Azure Monitor setup, KQL queries, failure event detection | End of Phase 3 |
| ai_rca.md | pgvector retrieval, prompt structure, RCA loop, incident schema | End of Phase 4/5 |
| api.md | FastAPI routes, auth, response schemas | End of Phase 5 |
| dashboard.md | React component design, data fetching patterns | End of Phase 6 |

## Runbooks

The `runbooks/` subfolder contains step-by-step operational guides.
These ARE written during the relevant phase because they are needed
immediately for operating the system.
