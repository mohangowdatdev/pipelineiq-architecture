# PipelineIQ — RCA Retrieval Design (navigation-first)

**Status: DESIGN / DIRECTION — not built.** Captured 2026-07-01 (S22). This
doc records an architectural direction agreed in discussion; it evolves the
original pure-pgvector RCA plan (`docs/ai_rca.md`, SCHEMA `iac_embeddings`) and
is logged as **DECISIONS #83**. Nothing here is implemented yet — Phases 3/4/5
will build it. This is the "don't lose the understanding" artifact; refine
before coding.

---

## 1. The problem this solves

When a pipeline fails, PipelineIQ's value is a **specific, code-grounded**
incident — not "ADF pipeline failed" but "the copy activity's dataset in
`bicep/adf/dataset_sql_source.bicep` references a `watermark_column` that no
longer exists — here's the block and the fix." To do that the AI (GPT-4o) must
read the **actual IaC / notebook source** relevant to *this* failure.

The question is **how the AI finds the right code** in a codebase that, for a
real client (a global manufacturing conglomerate), may be tens of thousands of
files across many repos.

## 2. The decision: retrieval is NAVIGATION, not similarity

The original plan was pure **embedding-first RAG**: chunk all IaC → embed →
store in `pgvector` → cosine-match the failure against chunks. We are pivoting
away from that as the *primary* mechanism, for three reasons:

1. **Vector matching is "number matching" and imperfect for code.** Embeddings
   encode learned *meaning* (so `watermark_column` ≈ `incremental copy config`
   even with no shared words) — genuinely better than keyword search. But for
   terse HCL/Bicep/config it's the weak case: chunk boundaries split context,
   near-duplicate blocks collide, and the true culprit may rank #8 when you
   pulled top-5. You don't know what you missed.
2. **Our failures are STRUCTURED.** Failure events (from Log Analytics /
   `pipeline.pipeline_exec_log` + Databricks/ADF diagnostics) carry the failing
   object's **exact identity** — pipeline name, notebook workspace path,
   function name, error class. So we usually don't need fuzzy search to *find*
   the culprit; the signal **is** the address. Retrieval becomes a routing
   problem, not a similarity problem.
3. **Navigation is scale-free.** A human (and a coding agent like Claude Code)
   doesn't read 10 files — it navigates to the right notebook/snippet and reads
   that + its dependencies. Cost is **O(one failure's blast radius)**, not
   O(repo). This is what makes it work "no matter how big the architecture is."

**Corpus-size reality check (Velora):** the *entire* PipelineIQ IaC is ~15,000
tokens (64 files, 3,605 lines) — trivially fits GPT-4o's 128K window (~$0.04 to
send all of it). So at *this* scale retrieval is barely needed at all; the
navigation design is chosen for **the conglomerate scale** and for being the
architecturally correct, portfolio-worthy pattern (agentic retrieval), not to
save Velora tokens.

## 3. The philosophy is already in the project: metadata-driven

The data plane is metadata-driven: `pipeline.entity_registry` describes the 12
entities, and a **generic** ADF pipeline reads that metadata to know what to do.
Engine generic; specifics in metadata.

**Do the same for RCA retrieval.** A **manifest** (a "component_registry" — the
code-plane analog of `entity_registry`) describes each client's conventions:
where each component type lives, how a runtime object name maps to a source
file, and how to discover that file's dependencies. The **engine stays generic**;
each client (Velora, the conglomerate) ships its own manifest. That manifest is
the artifact produced by `docs/runbooks/new_client_onboarding.md`.

## 4. Architecture — a navigator with progressive fallback

Four layers, each a fallback for the one before:

**Layer 1 — Router (deterministic, cheap, ~80% of cases).**
Failure event → `{component_type, object_ref, symbols, error_kind}`. Because the
signal names the object, this is a lookup, not a guess. Consults the manifest →
concrete source file path(s).

**Layer 2 — Dependency expander ("and its dependency codes").**
From the located file, follow **explicit** references (bounded depth 1–2):
`dbutils.notebook.run(...)`, ADF linked-service/dataset refs, Terraform `module`
sources, Python imports, `spark.table("…")` read/write lineage. `error_kind`
decides *which* edges matter (schema error → follow data lineage; auth error →
follow linked-service/credential config). Result: the failing artifact + its
immediate collaborators — the context a human assembles.

**Layer 3 — Agentic navigation (the messy tail).**
When the manifest doesn't cleanly resolve (vague error, cross-repo dep with no
explicit import, a convention the manifest didn't capture), hand GPT-4o
**read-only tools** (`grep`, `read_file`, `list_dir`, `follow_ref`) seeded with
the manifest as a *hint*, and let it navigate like a coding agent — grep for the
entity name, read what it finds, follow refs, stop when it has enough. This is
literally the Claude Code loop, and it's what makes the system adapt to any repo
at runtime instead of needing a perfect manifest.

**Layer 4 — pgvector semantic fallback (optional, long tail only).**
DEMOTED from primary to last resort: the one job structural navigation can't do
is match on *meaning* when there's no name/path anchor ("what code relates to
this concept?"). `iac_embeddings` + cosine search stays in the design for that
case — and as an honest demo of "we know when RAG is right vs. when navigation
is right." It is no longer the main road.

## 5. Worked example (real Velora files)

**Scenario:** schema drift — source renames `velora_oms.orders.total_amount` →
`order_amount` (a `failure_injector.py` scenario). Bronze is schema-agnostic so
it ingests fine; `notebooks/silver/build_silver_orders.py` still selects
`total_amount` → the nightly Databricks job dies.

```
Step 0 — signal (from pipeline_exec_log + Databricks diag in Log Analytics):
  { source: "databricks",
    workspace_path: "/Shared/pipelineiq/silver/build_silver_orders",
    error_class: "org.apache.spark.sql.AnalysisException",
    error_text: "UNRESOLVED_COLUMN 'total_amount' … Did you mean 'order_amount'?" }

Step 1 — Router → intent:
  { component_type:"databricks", object_ref:"/Shared/.../build_silver_orders",
    symbols:["total_amount","order_amount"], error_kind:"SCHEMA_MISMATCH" }

Step 2 — Manifest name_resolution (regex):
  "^/Shared/pipelineiq/(?P<layer>\w+)/(?P<name>.+)$"  +  "notebooks/{layer}/{name}.py"
  → notebooks/silver/build_silver_orders.py           # deterministic address translation
  grep line 47:  .select("order_id","customer_id","total_amount","order_date")

Step 3 — Dependency expander (error_kind=SCHEMA_MISMATCH → follow data lineage):
  reads regex on the file → "bronze.orders"
  → notebooks/bronze/ingest_to_bronze.py     (producer — schema-agnostic passthrough)
  → SCHEMA.md orders                          (contract: total_amount decimal(12,2))
  → source_schema_diff_24h                    (RENAME total_amount → order_amount)

  Context assembled = 3 files' relevant snippets + error. NOT 64 files.
  On a 60,000-file monorepo this number is identical.

Step 4 — GPT-4o call: system(RCA) + failure_event + evidence + forced JSON schema
  → { root_cause_summary, affected_component, evidence[], suggested_fix, confidence }
  → pipelineiq.incident_store (append-only) + Slack webhook

Step 5 — human approves suggested_fix via /v1/pipelines/{id}/approve-fix → opens a PR
```

Every retrieval step was navigation. pgvector was not touched.

**Where the fuzzy layers kick in instead:** a vague `Py4JJavaError: executor
lost` (no column, no clear object) → Layer 3 agent greps cluster config, reads
`core/medallion_workflow/main.tf`, infers OOM on the 7M-row inventory silver.
No structural anchor at all → Layer 4 pgvector.

## 6. The "playground" — where the AI greps/reads

The AI needs a real filesystem to navigate (today, in-session, that's Claude
Code ↔ the Mac; in prod it's service-owned). Shape:

```
GPT-4o  --tool calls-->  FastAPI RCA service (Azure Container Apps)
   grep / read_file / list_dir / follow_ref  (all READ-ONLY Python functions,
   each shells `ripgrep` / opens a file)
                          |
                          v
   /repos  = a git checkout of the client's IaC + notebook repos, on disk
```

- **Source of truth:** Azure DevOps Repos. The RCA service holds a synced mirror.
- **Kept fresh by the DevOps push webhook already in the architecture**
  (`Azure DevOps → FastAPI webhook`). Original plan used it only to feed
  pgvector; extend it to also `git pull` the checkout (and re-embed changed
  files for the Layer-4 fallback). **This is the one real gap the discussion
  surfaced — small and clean.**
- **Physical home (reco):** an **Azure Files** volume mounted on the Container
  App — persistent `/repos` shared across replicas, survives cold starts.
  (Alt: ephemeral re-clone on cold start — fine for demo, wasteful at scale.)
- **Conglomerate scale:** don't clone the monorepo — **sparse-checkout only the
  paths the manifest declares** (`pipelines/`, `notebooks/`, `functions/`),
  shallow (`--depth 1`). The manifest scopes the clone; 60k files → a few
  hundred.
- **Alt without a checkout:** hit the Azure DevOps REST API (list/read/search)
  so "grep" = API calls, no local disk. Weaker than local `ripgrep` (no true
  regex, rate limits, per-call latency, awkward dep-following). Use only if
  persistent storage is off the table or the monorepo is too big even to sparse
  checkout.

## 7. Safety: read-only diagnosis, write-gated fixes

Navigation is **read-only** (`grep`/`read_file`/`list_dir` — no write, no code
execution, no Azure access). A hallucinating model can only "read the wrong
file," so the container itself is the isolation boundary — **no per-incident
microVM needed.** The *dangerous* part (applying a fix) is a separate,
write-capable path already gated in the architecture: the AI proposes a
`suggested_fix`; a human approves via `/v1/pipelines/{id}/approve-fix`, which
opens a **PR** against DevOps. Read-heavy diagnosis, permission-gated write.

## 8. Architecture-agnostic proof (swap one file)

Conglomerate Databricks failure arrives as `/prod/finance/transforms/dim_gl_account`.
Change **zero engine code** — write `manifests/conglomerate.yaml`:

```yaml
databricks:
  name_resolution:
    pattern: "^/prod/(?P<domain>\w+)/transforms/(?P<name>.+)$"
    path_template: "src/{domain}/notebooks/{name}.sql"
```

Same router, same expander, same LLM call. The manifest absorbs their
conventions; the engine stays generic.

## 9. Open questions / next discussions (S23+)

- **Main dial: agentic (Layer 3) vs. deterministic (Layer 1) balance.** Trades
  upfront engineering (hand-built routers/parsers) against runtime token cost
  (let the LLM navigate). Recommendation leaning: deterministic for the known
  taxonomy, agentic for the tail — decide before building.
- **Manifest schema** — formal spec of `component_registry` (per component:
  repo, path glob, name_resolution regex, lineage rules). Auto-discovery vs.
  hand-authored vs. hybrid (scan repo → propose → human-review).
- **Failure taxonomy** — enumerate `error_kind`s and map each of the 6
  `failure_injector.py` scenarios into it; each kind declares which dependency
  edges to follow.
- **Dependency parsers** — per language/tool (PySpark `notebook.run`, ADF
  linked-service/dataset refs, Terraform `module`) vs. leaning on Layer 3 to
  follow refs by reading. Probably hybrid.
- **Where the RCA loop lives** — `fastapi/rca_loop.py` (build_order 7.10):
  KQL poll → route → navigate → GPT-4o → `incident_store` → Slack, ≤5 min.
- **pgvector's real weight** — keep as genuine Layer-4 fallback, or reduce to a
  demo-only showcase? Decide once Layers 1–3 are prototyped.
- **User has more questions / understanding to add** — this doc is a living
  capture; extend it next session before any code.

## 10. Relationship to existing docs

- `docs/ai_rca.md` — the original RCA-loop doc (still to be written at end of
  Phase 4/5). This design REFRAMES its retrieval half from embedding-first to
  navigation-first. Reconcile the two when `ai_rca.md` is finalised.
- SCHEMA `pipelineiq.iac_embeddings` — retained, but now backs **Layer 4** only.
- `pipeline.entity_registry` — the philosophical template for the manifest.
- DECISIONS #1 (pgvector as the featured pattern) — softened, not reversed:
  pgvector stays, demoted to semantic fallback. See DECISIONS #83.
