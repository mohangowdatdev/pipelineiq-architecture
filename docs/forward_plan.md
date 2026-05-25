# PipelineIQ — Forward Plan

Session-level operational sequencing for the work that remains after
S14 (inventory migration to Databricks). Companion to:

- **`PLANNING.md`** — the architectural truth (full stack, gold model, failure scenarios, phase exit criteria, phase dependencies).
- **`docs/build_order.md`** — resource-level provisioning status (single source of truth for "what exists in Azure").
- **`PROGRESS.md` `## Next task`** — the immediate next-session pickup point.

This file is the bridge between them: it sequences the remaining
phases into discrete sessions so a future me (or another collaborator)
can pick up cleanly without re-deriving the order.

---

## Where we are (S14 wrap, 2026-05-25)

| Layer | State |
|---|---|
| Source generator (Function) | ✅ Live, autonomous, **inventory write removed** |
| Inventory writer (Databricks Job) | ✅ Live, autonomous, 00:35 UTC daily |
| Source DB `velora_oms` | ✅ 24 days continuous (2026-04-27 → 2026-05-24) |
| Medallion (bronze + silver + gold) | ⚠️ **11 days behind source** (through 2026-05-13) — needs catch-up |
| Postgres `pipeline.*` schema | ⚠️ Provisioned + seeded, **architecturally orphaned** (no consumer) |
| Postgres `pipelineiq.*` schema (incident_store + iac_embeddings) | ⚠️ Tables exist, 0 rows |
| Function REST endpoints | ❌ Architected, never written |
| ADF (Tier 6) | ❌ Not started |
| pgvector chunker + IaC webhook | ❌ Not started (Phase 4) |
| Failure injection + RCA loop | ❌ Not started (Phase 3) |
| FastAPI backend | ❌ Not started (Phase 5) |
| React dashboard | ❌ Not started (Phase 6) |

The Function App + Databricks Job pair is the only thing running
autonomously today. Everything downstream is still manual.

---

## Dependency graph (recap from PLANNING.md)

```
Phase 0 ──┬── Phase 1 (Generator) ──┐
          │                          │
          └── Tier 6 (ADF + metadata)┼── Phase 3 (RCA loop)
          │                          │
          └── Phase 4 (pgvector) ────┘
                                     │
                                     └── Phase 5 (FastAPI)
                                              │
                                              └── Phase 6 (React)
```

Two independent paths converge into Phase 3:
- **Tier 6 path:** ADF + Function REST endpoints + `pipeline.*` activation → real failure signals in `pipeline_exec_log`.
- **Phase 4 path:** Azure OpenAI embeddings deployment + IaC chunker + DevOps webhook → retrieval context in `iac_embeddings`.

Phase 3 needs both. After that, Phase 5 → Phase 6 is linear.

---

## Session-by-session outline

Roughly 7 sessions to "demo-ready end-to-end" from S14 wrap. Times are
rough — each block can stretch or compress by half a session based on
how much yak-shaving comes up.

### S15 — Catch-up + canonical proof
**Objective:** sync medallion to source DB; verify the autonomous two-writer pair works under the new architecture.

1. Verify 2026-05-26 00:30 + 00:35 UTC autonomous fire — `scripts/audit_fires.py` shows 2026-05-25 with ~400 orders + 189,225 inventory.
2. **Medallion catch-up for 11 days (5/14 → 5/24)** — `export_velora_to_landing.py --start 2026-05-14 --end 2026-05-24` then bronze/silver/gold smoke per entity. ~45-60 min wall.
3. Validate dim_customer SCD-2 holds at 0 collisions after the catch-up wave.
4. Pre-flight for S16: confirm Azure OpenAI quota + KV permissions + DevOps service principal exists.

Exit: medallion at 24 days; autonomous fire proven; dim_customer clean.

### S16 — Tier 6 ADF + Function REST endpoints (chunk 1 of 2)
**Objective:** stand up ADF + the Function REST endpoints that ADF will consume.

1. **Function REST endpoints** (build_order 4.8): write `get_watermark`, `commit_watermark`, `register_file`, `log_run_start`, `log_run_end` as a new function group under the existing Function App. Unit-test against Postgres locally. Deploy. Smoke each via `curl` with function key.
2. **ADF resource** (6.1) — Terraform.
3. **Linked services** (6.2) — 4 Bicep files: Azure SQL, ADLS, Key Vault, Databricks.
4. **Parameterised datasets** (6.3) — Bicep, parameterised by `(schema, table, watermark_column, load_type)` from `entity_registry`.

Exit: ADF UI shows the linked services + datasets; `curl` against the 5 Function endpoints returns expected JSON.

### S17 — Tier 6 ADF master pipeline + metadata activation (chunk 2 of 2)
**Objective:** make ADF the consumer of `pipeline.*` and the production fire path for landing.

1. **Master parameterised copy pipeline** (6.4) — ForEach over `entity_registry`. Each iteration: `get_watermark` → copy SQL → landing → `register_file` → `commit_watermark`. Per-entity error handling routes to `log_run_end(status=failed)`.
2. **Databricks notebook activities** (6.5) — chain bronze → silver → gold per entity.
3. **Diagnostic settings** (6.6) — ADF pipeline runs stream to `pipelineiq-logs-dev`. Required for Phase 3.
4. **Cutover** (6.11) — schedule ADF master pipeline at 00:40 UTC daily (10 min after Function fire, 5 min after Databricks Job). Decommission `export_velora_to_landing.py` from production (keep as manual recovery).
5. **Validate** (build_order 6.7–6.10): pick a date, fire the ADF master pipeline manually, confirm `pipeline_exec_log` + `file_registry` + `watermarks` all have rows.

Exit: "metadata-driven" is no longer a slide. Every ADF run touches the four `pipeline.*` tables. Architecture-vs-reality gap (CLAUDE.md) closes.

### S18 — Phase 4 — pgvector IaC embeddings
**Objective:** populate `pipelineiq.iac_embeddings` from `PipelineIQ-IaC/main`.

1. **Azure OpenAI embeddings deployment** (build_order 7.7) — `text-embedding-3-small` (1536-dim, cheaper than `-large`; can swap if quality matters).
2. **IaC chunker** (7.8) — Python script (initially CLI, eventually a FastAPI route) that walks the IaC repo, chunks each .tf / .bicep file (~500 tokens, 50-token overlap), embeds via Azure OpenAI, UPSERTs to `iac_embeddings`.
3. **Validate** — `SELECT file_path, resource_type, vector_dims(embedding) FROM pipelineiq.iac_embeddings LIMIT 5;` should show ~80-120 rows.
4. **Azure DevOps webhook → chunker** (7.9) — defer the webhook side to S20 (paired with FastAPI). For S18, manual re-chunk after each IaC push is fine.

Exit: ~80-120 IaC chunks embedded + searchable via pgvector cosine similarity. Top-K query (K=5) returns sensible results for a few sample queries ("how is Postgres provisioned?", "what does the ADF linked service look like?").

### S19 — Phase 3 — Failure injection + RCA loop
**Objective:** end-to-end the failure detection + RCA + Slack alert loop.

1. **Slack webhook URL in KV** (build_order 8.5) — Slack workspace setup, paste URL into KV.
2. **KQL ingestion** — Python script polls Log Analytics every 60s, looks for ADF run failures + Databricks job failures. State: last_seen_ts in `pipelineiq.scan_cursor` (new table).
3. **RCA loop** (7.10) — for each new failure: pull top-K IaC chunks via pgvector (using the failed resource's name as the query), grab last 50 raw log lines from LA, call GPT-4o with a structured-output JSON schema (root_cause_summary, affected_component, evidence, suggested_fix, confidence).
4. **`incident_store` write** — UPSERT incident row. Append-only — no UPDATE, no DELETE.
5. **Slack POST** — formatted message with summary + link to LA query.
6. **Failure injection verification** — inject each of the 6 scenarios via the generator's `--failure` flag, verify incident lands within 5 min, evidence quality is human-readable.

Exit: all 6 failure scenarios produce incident rows + Slack alerts. RCA quality is good enough to demo (root_cause_summary is correct ≥4/6 scenarios).

### S20 — Phase 5 — FastAPI backend on Container Apps
**Objective:** wrap the S19 logic in a real backend service.

1. **Container Apps environment + Container App** (build_order 7.4–7.6) — Terraform.
2. **FastAPI scaffold** — `fastapi/main.py` with `/v1/incidents`, `/v1/pipelines/{run_id}/status`, `/v1/iac/chunks`, `/v1/webhooks/iac`.
3. **Move S19's KQL poller + RCA loop into a FastAPI background task** (or a separate Container App cron job).
4. **Move S18's chunker into the FastAPI `/v1/webhooks/iac` route** (build_order 7.9) — Azure DevOps service connection + webhook secret, HMAC verification.
5. **Logs stream to `pipelineiq-logs-dev`**.

Exit: `curl https://pipelineiq-fastapi-dev.../v1/incidents` returns the 6 injected scenarios. Manual IaC commit → webhook fires → embeddings updated. Slack alerts continue to fire from the new home.

### S21 — Phase 6 — React dashboard + demo polish
**Objective:** the user-facing surface.

1. **Static Web Apps + React scaffold** (build_order 8.1).
2. **Three views**: pipeline timeline, incident timeline, per-incident detail with evidence + IaC chunks + suggested fix.
3. **AAD auth** (optional) — Static Web Apps built-in identity is enough for a single dev.
4. **Demo dry-run** — trigger a failure → watch it propagate through ADF → Phase 3 → Phase 5 → React UI + Slack within 5 min.
5. **Demo recording / screenshots for portfolio**.

Exit: project is demo-ready. CLAUDE.md "Architecture vs reality" table is fully ✅.

---

## The 8 cross-phase sub-items (don't lose these)

Items that are part of the architecture but easy to overlook because
they don't have a phase name attached. Each is captured in
`docs/build_order.md` — listed here for narrative.

| # | Item | Slots into |
|---|---|---|
| 1 | Function REST endpoints (`get_watermark` / `commit_watermark` / `register_file` / `log_run_*`) | S16 (Tier 6 chunk 1) |
| 2 | `pipeline.*` activation — ADF actually consumes `entity_registry`, writes `watermarks` / `file_registry` / `pipeline_exec_log` | S17 (Tier 6 chunk 2) |
| 3 | ADF → Databricks orchestration trigger — ADF Web Activity fires bronze notebook on landing event | S17 |
| 4 | Azure DevOps webhook → IaC chunker | S20 (paired with FastAPI) — chunker itself in S18 |
| 5 | Slack incoming webhook URL in Key Vault | S19 (Phase 3 prerequisite) |
| 6 | Azure OpenAI embeddings deployment (`text-embedding-3-small`) | S18 (Phase 4 prerequisite) |
| 7 | Container Apps environment + custom domain + secrets pipeline | S20 (Phase 5 setup) |
| 8 | Medallion catch-up cadence — what to do when bronze/silver/gold lag source | S15 (one-time); see runbook below |

---

## Operational follow-ups (small, can interleave anywhere)

| Item | Effort | Where |
|---|---|---|
| Stale `.env` (`pipelineiq-sql-dev` → `pipelineiq-sql-velora-dev`) | 1 min | next session start |
| Function timeout 30m → 5m (inventory write is out) | 1 deploy cycle | S16 (when Function REST endpoints deploy anyway) |
| Retire `scripts/inventory_only.py` in favour of `run_inventory_smoke.py` | 5 min | after S15 catch-up settles |
| `propagate=False` for OT logger to fix duplicate AppTraces | 1 deploy cycle | S16 |
| Generator `--dry-run` mode is broken (item 9.1) | 30 min | any time |
| Repos eventually push to `mohangowdat-sail` for company handoff | — | future one-time event |

---

## How to keep this file current

Update at end of each session that touches phase-level work:

1. Mark the session row above as "done" or "in progress" or note the actual scope (might split a session, might combine two).
2. Move closed sub-items from the "8 cross-phase" table into `build_order.md` as Done rows.
3. Add new sub-items here as they surface — don't let them live only in the chat or a session log.
4. When a phase exits per the criteria in `PLANNING.md` `## Phase-by-phase exit criteria`, write a one-paragraph wrap above its dedicated `docs/{phase}.md` doc.

---

*Created 2026-05-25 (S14 wrap). Update on every session that closes a phase milestone.*
