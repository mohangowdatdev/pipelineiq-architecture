-- PipelineIQ — PostgreSQL Control Plane Bootstrap
-- Creates pipeline + pipelineiq schemas, all control tables, pgvector extension,
-- and seeds entity_registry with all 10 Velora source entities.
--
-- Idempotent: uses IF NOT EXISTS / CREATE OR REPLACE throughout.
-- Safe to run multiple times.
--
-- Run order:
--   1. Ensure PostgreSQL Flexible Server is running and accessible
--   2. Run: psql $POSTGRES_URL -f scripts/bootstrap_postgres.sql
--   3. Verify: psql $POSTGRES_URL -c "SELECT entity_name FROM pipeline.entity_registry"
--
-- Prerequisites:
--   PostgreSQL 14+ with pgvector extension available
--   (Enabled on Azure PostgreSQL Flexible Server via the extensions list in the portal)

-- ── Extensions ────────────────────────────────────────────────────────────────

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector";

-- ── Schemas ───────────────────────────────────────────────────────────────────

CREATE SCHEMA IF NOT EXISTS pipeline;
CREATE SCHEMA IF NOT EXISTS pipelineiq;

-- ── pipeline.entity_registry ──────────────────────────────────────────────────
-- Drives the entire ADF pipeline. One row per source entity.
-- Adding a new source table = one INSERT here, zero changes to ADF pipelines.

CREATE TABLE IF NOT EXISTS pipeline.entity_registry (
    entity_id           SERIAL          PRIMARY KEY,
    entity_name         VARCHAR(100)    NOT NULL UNIQUE,    -- 'velora_oms.orders'
    source_schema       VARCHAR(50)     NOT NULL,            -- 'velora_oms'
    source_table        VARCHAR(100)    NOT NULL,            -- 'orders'
    watermark_column    VARCHAR(50)     NOT NULL,            -- 'updated_at'
    load_type           VARCHAR(20)     NOT NULL DEFAULT 'incremental',  -- 'incremental' | 'full'
    schedule            VARCHAR(20)     NOT NULL DEFAULT 'daily',
    active              BOOLEAN         NOT NULL DEFAULT TRUE,
    priority            INT             NOT NULL DEFAULT 5,
    depends_on          VARCHAR(100)    NULL                 -- entity_name of upstream dependency
);

-- ── pipeline.watermarks ───────────────────────────────────────────────────────
-- One row per entity per environment. Watermarks advance only on success.
-- Failure leaves watermark unchanged — next run retries the same window.

CREATE TABLE IF NOT EXISTS pipeline.watermarks (
    watermark_id            SERIAL          PRIMARY KEY,
    entity_name             VARCHAR(100)    NOT NULL,
    environment             VARCHAR(20)     NOT NULL DEFAULT 'dev',
    last_successful_load    TIMESTAMPTZ     NOT NULL,
    next_window_start       TIMESTAMPTZ     NOT NULL,
    updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    UNIQUE (entity_name, environment)
);

-- ── pipeline.process_queue ────────────────────────────────────────────────────
-- ADF reads this to determine which entities to process and in what order.

CREATE TABLE IF NOT EXISTS pipeline.process_queue (
    queue_id            SERIAL          PRIMARY KEY,
    entity_name         VARCHAR(100)    NOT NULL,
    scheduled_run_time  TIMESTAMPTZ     NOT NULL,
    status              VARCHAR(20)     NOT NULL DEFAULT 'pending',  -- pending|running|complete|failed
    retry_count         INT             NOT NULL DEFAULT 0,
    last_heartbeat      TIMESTAMPTZ     NULL,
    run_id              VARCHAR(100)    NULL,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_process_queue_status
    ON pipeline.process_queue (status, scheduled_run_time);

-- ── pipeline.file_registry ────────────────────────────────────────────────────
-- Records every file landed in ADLS landing/ by ADF Copy Activity.

CREATE TABLE IF NOT EXISTS pipeline.file_registry (
    file_id             SERIAL          PRIMARY KEY,
    file_path           TEXT            NOT NULL,
    source_entity       VARCHAR(100)    NOT NULL,
    landed_at           TIMESTAMPTZ     NOT NULL,
    row_count           INT             NOT NULL,
    pipeline_run_id     VARCHAR(100)    NOT NULL,
    processed_flag      BOOLEAN         NOT NULL DEFAULT FALSE,
    processed_at        TIMESTAMPTZ     NULL
);

CREATE INDEX IF NOT EXISTS idx_file_registry_entity_processed
    ON pipeline.file_registry (source_entity, processed_flag);

-- ── pipeline.pipeline_exec_log ────────────────────────────────────────────────
-- One row per pipeline run, opened on start, closed on end.
-- Mutation policy: log_run_start INSERTs; log_run_end UPDATEs the matching
-- open row (end_time IS NULL) once. No re-opens, no deletes.

CREATE TABLE IF NOT EXISTS pipeline.pipeline_exec_log (
    log_id              SERIAL          PRIMARY KEY,
    run_id              VARCHAR(100)    NOT NULL,
    pipeline_name       VARCHAR(200)    NOT NULL,
    entity_name         VARCHAR(100)    NULL,
    start_time          TIMESTAMPTZ     NOT NULL,
    end_time            TIMESTAMPTZ     NULL,
    status              VARCHAR(20)     NOT NULL DEFAULT 'running',  -- running|success|failed
    rows_read           INT             NULL,
    rows_written        INT             NULL,
    rows_rejected       INT             NULL,
    error_message       TEXT            NULL,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_exec_log_entity_time
    ON pipeline.pipeline_exec_log (entity_name, start_time DESC);

CREATE INDEX IF NOT EXISTS idx_exec_log_status
    ON pipeline.pipeline_exec_log (status, start_time DESC);

-- ── pipelineiq.incident_store ─────────────────────────────────────────────────
-- AI-generated incident records. Append-only — never UPDATE or DELETE.
-- Each row is a complete, immutable RCA artefact.

CREATE TABLE IF NOT EXISTS pipelineiq.incident_store (
    incident_id             UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_id             VARCHAR(200)    NOT NULL,
    failure_timestamp       TIMESTAMPTZ     NOT NULL,
    root_cause_summary      TEXT            NOT NULL,
    affected_component      VARCHAR(200)    NOT NULL,
    evidence                JSONB           NOT NULL DEFAULT '[]',   -- array of evidence strings
    suggested_fix           TEXT            NULL,
    confidence              VARCHAR(10)     NOT NULL DEFAULT 'medium', -- high|medium|low
    iac_chunks_used         JSONB           NOT NULL DEFAULT '[]',   -- array of {file_path, resource_type, content}
    raw_logs_used           TEXT            NULL,
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW()
    -- Append-only. Never UPDATE or DELETE.
);

CREATE INDEX IF NOT EXISTS idx_incident_store_pipeline
    ON pipelineiq.incident_store (pipeline_id, failure_timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_incident_store_created
    ON pipelineiq.incident_store (created_at DESC);

-- ── pipelineiq.iac_embeddings ─────────────────────────────────────────────────
-- IaC chunks embedded by Azure OpenAI text-embedding-ada-002.
-- Queried at RCA time using cosine similarity to retrieve relevant Terraform/Bicep blocks.
-- Populated by the FastAPI IaC ingest webhook on every push to main branch.

CREATE TABLE IF NOT EXISTS pipelineiq.iac_embeddings (
    id                  SERIAL          PRIMARY KEY,
    file_path           TEXT            NOT NULL,
    resource_type       VARCHAR(100)    NOT NULL,   -- 'azurerm_data_factory_pipeline' etc
    branch              VARCHAR(100)    NOT NULL DEFAULT 'main',
    content             TEXT            NOT NULL,   -- original Terraform/Bicep block
    embedding           vector(1536)    NOT NULL,   -- Azure OpenAI text-embedding-ada-002 output
    ingested_at         TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

-- ivfflat index for cosine similarity search.
-- lists = 100 is appropriate for up to ~1M rows.
-- Rebuild index with VACUUM ANALYZE after bulk ingest.
CREATE INDEX IF NOT EXISTS idx_iac_embeddings_vector
    ON pipelineiq.iac_embeddings
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

CREATE INDEX IF NOT EXISTS idx_iac_embeddings_resource_type
    ON pipelineiq.iac_embeddings (resource_type);

-- ── Seed entity_registry ──────────────────────────────────────────────────────
-- 10 Velora source entities. ADF pipeline is config-driven by this table.
-- Priority determines processing order within a run.
-- depends_on enforces that upstream entities complete before downstream ones start.

INSERT INTO pipeline.entity_registry
    (entity_name, source_schema, source_table, watermark_column,
     load_type, schedule, active, priority, depends_on)
VALUES
    -- OMS entities (orders must precede order_lines and status_log)
    ('velora_oms.orders',               'velora_oms', 'orders',               'updated_at',   'incremental', 'daily', TRUE, 1, NULL),
    ('velora_oms.order_lines',          'velora_oms', 'order_lines',          'updated_at',   'incremental', 'daily', TRUE, 2, 'velora_oms.orders'),
    ('velora_oms.order_status_log',     'velora_oms', 'order_status_log',     'created_at',   'incremental', 'daily', TRUE, 3, 'velora_oms.orders'),
    -- CRM entities (customers must precede addresses)
    ('velora_crm.customers',            'velora_crm', 'customers',            'updated_at',   'incremental', 'daily', TRUE, 1, NULL),
    ('velora_crm.customer_addresses',   'velora_crm', 'customer_addresses',   'updated_at',   'incremental', 'daily', TRUE, 2, 'velora_crm.customers'),
    -- PIM entities (products must precede pricing; inventory is full refresh)
    ('velora_pim.products',             'velora_pim', 'products',             'updated_at',   'incremental', 'daily', TRUE, 1, NULL),
    ('velora_pim.product_pricing',      'velora_pim', 'product_pricing',      'created_at',   'incremental', 'daily', TRUE, 2, 'velora_pim.products'),
    ('velora_pim.inventory_snapshot',   'velora_pim', 'inventory_snapshot',   'snapshot_date','full',        'daily', TRUE, 3, NULL),
    -- HRM entities (reps must precede assignments)
    ('velora_hrm.sales_reps',           'velora_hrm', 'sales_reps',           'updated_at',   'incremental', 'daily', TRUE, 1, NULL),
    ('velora_hrm.territory_assignments','velora_hrm', 'territory_assignments', 'created_at',  'incremental', 'daily', TRUE, 2, 'velora_hrm.sales_reps'),
    -- Static reference seeds (added in S9.5 / DECISIONS #60; seeded into
    -- entity_registry retroactively in S13). No real watermark — these
    -- tables only change when the generator/seed code changes.
    ('velora_pim.product_categories',   'velora_pim', 'product_categories',   'category_id',  'full',        'daily', TRUE, 11, NULL),
    ('velora_pim.stores',               'velora_pim', 'stores',               'opened_date',  'full',        'daily', TRUE, 12, NULL)
ON CONFLICT (entity_name) DO NOTHING;

-- ── Seed initial watermarks ───────────────────────────────────────────────────
-- Start date: 2025-01-01. Generator backfill should be run from this date.
-- The watermark system will advance these automatically on successful runs.

INSERT INTO pipeline.watermarks
    (entity_name, environment, last_successful_load, next_window_start)
VALUES
    ('velora_oms.orders',               'dev', '2024-12-31 00:00:00+00', '2025-01-01 00:00:00+00'),
    ('velora_oms.order_lines',          'dev', '2024-12-31 00:00:00+00', '2025-01-01 00:00:00+00'),
    ('velora_oms.order_status_log',     'dev', '2024-12-31 00:00:00+00', '2025-01-01 00:00:00+00'),
    ('velora_crm.customers',            'dev', '2024-12-31 00:00:00+00', '2025-01-01 00:00:00+00'),
    ('velora_crm.customer_addresses',   'dev', '2024-12-31 00:00:00+00', '2025-01-01 00:00:00+00'),
    ('velora_pim.products',             'dev', '2024-12-31 00:00:00+00', '2025-01-01 00:00:00+00'),
    ('velora_pim.product_pricing',      'dev', '2024-12-31 00:00:00+00', '2025-01-01 00:00:00+00'),
    ('velora_pim.inventory_snapshot',   'dev', '2024-12-31',             '2025-01-01'),
    ('velora_hrm.sales_reps',           'dev', '2024-12-31 00:00:00+00', '2025-01-01 00:00:00+00'),
    ('velora_hrm.territory_assignments','dev', '2024-12-31 00:00:00+00', '2025-01-01 00:00:00+00'),
    -- Static seeds (S13 retroactive seed)
    ('velora_pim.product_categories',   'dev', '2024-12-31 00:00:00+00', '2025-01-01 00:00:00+00'),
    ('velora_pim.stores',               'dev', '2024-12-31 00:00:00+00', '2025-01-01 00:00:00+00')
ON CONFLICT (entity_name, environment) DO NOTHING;

\echo 'Bootstrap PostgreSQL complete.'
\echo 'Verify with: SELECT entity_name, watermark_column, load_type FROM pipeline.entity_registry ORDER BY entity_name;'
