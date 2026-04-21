-- PipelineIQ — Azure SQL Database Bootstrap
-- Velora Retail Group source schema: 4 schemas, 10 tables + 1 control table
--
-- Idempotent: uses IF NOT EXISTS throughout.
-- Safe to run multiple times — will not drop or truncate existing data.
--
-- Run order:
--   1. Ensure Azure SQL Database is provisioned and accessible
--   2. Set environment variables: AZURE_SQL_SERVER, AZURE_SQL_DATABASE, etc.
--   3. Run: sqlcmd -S $AZURE_SQL_SERVER -d $AZURE_SQL_DATABASE -U $AZURE_SQL_USERNAME -P $AZURE_SQL_PASSWORD -i scripts/bootstrap_sql.sql
--
-- After running this script, run the generator to seed catalogue data:
--   cd generator && python main.py --date 2025-01-01 --dry-run

-- ── Schema creation ───────────────────────────────────────────────────────────

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'velora_oms')
    EXEC('CREATE SCHEMA velora_oms');
GO

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'velora_crm')
    EXEC('CREATE SCHEMA velora_crm');
GO

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'velora_pim')
    EXEC('CREATE SCHEMA velora_pim');
GO

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'velora_hrm')
    EXEC('CREATE SCHEMA velora_hrm');
GO

-- ── velora_pim.product_categories ────────────────────────────────────────────
-- Static reference table. Not extracted by ADF (never changes after seeding).
-- Seeded by catalogue.py on first generator run.

IF NOT EXISTS (SELECT 1 FROM sys.objects WHERE object_id = OBJECT_ID(N'velora_pim.product_categories') AND type = 'U')
BEGIN
    CREATE TABLE velora_pim.product_categories (
        category_id     NVARCHAR(36)    NOT NULL,
        category_name   NVARCHAR(200)   NOT NULL,
        sub_category    NVARCHAR(200)   NULL,
        division        NVARCHAR(50)    NOT NULL,
        is_active       BIT             NOT NULL DEFAULT 1,
        CONSTRAINT PK_product_categories PRIMARY KEY (category_id)
    );
    PRINT 'Created velora_pim.product_categories';
END
GO

-- ── velora_pim.stores ─────────────────────────────────────────────────────────
-- Static store master. Not extracted by ADF.
-- Seeded by catalogue.py on first generator run.

IF NOT EXISTS (SELECT 1 FROM sys.objects WHERE object_id = OBJECT_ID(N'velora_pim.stores') AND type = 'U')
BEGIN
    CREATE TABLE velora_pim.stores (
        store_id        NVARCHAR(20)    NOT NULL,
        store_name      NVARCHAR(200)   NOT NULL,
        city            NVARCHAR(100)   NOT NULL,
        state           NVARCHAR(100)   NOT NULL,
        territory_id    NVARCHAR(36)    NOT NULL,
        store_tier      NVARCHAR(20)    NOT NULL,    -- FLAGSHIP | STANDARD | EXPRESS
        is_active       BIT             NOT NULL DEFAULT 1,
        opened_date     DATE            NULL,
        CONSTRAINT PK_stores PRIMARY KEY (store_id)
    );
    PRINT 'Created velora_pim.stores';
END
GO

-- ── velora_oms.orders ─────────────────────────────────────────────────────────
-- ADF watermark: updated_at (incremental)

IF NOT EXISTS (SELECT 1 FROM sys.objects WHERE object_id = OBJECT_ID(N'velora_oms.orders') AND type = 'U')
BEGIN
    CREATE TABLE velora_oms.orders (
        order_id        NVARCHAR(36)    NOT NULL,
        customer_id     NVARCHAR(36)    NOT NULL,
        channel_type    NVARCHAR(20)    NOT NULL,   -- D2C | B2B | STORE
        order_date      DATE            NOT NULL,
        status          NVARCHAR(30)    NOT NULL,   -- PENDING|PROCESSING|SHIPPED|DELIVERED|CANCELLED|RETURNED|RETURN_INITIATED
        store_id        NVARCHAR(20)    NULL,       -- populated for STORE channel only
        rep_id          NVARCHAR(36)    NULL,       -- populated for B2B channel only
        total_amount    DECIMAL(12,2)   NOT NULL,
        currency        NVARCHAR(3)     NOT NULL DEFAULT 'INR',
        created_at      DATETIME2       NOT NULL DEFAULT GETUTCDATE(),
        updated_at      DATETIME2       NOT NULL DEFAULT GETUTCDATE(),
        source_system   NVARCHAR(20)    NOT NULL DEFAULT 'VELORA_OMS',
        CONSTRAINT PK_orders PRIMARY KEY (order_id)
    );
    CREATE INDEX IX_orders_updated_at  ON velora_oms.orders (updated_at);
    CREATE INDEX IX_orders_customer_id ON velora_oms.orders (customer_id);
    CREATE INDEX IX_orders_status      ON velora_oms.orders (status);
    PRINT 'Created velora_oms.orders';
END
GO

-- ── velora_oms.order_lines ────────────────────────────────────────────────────
-- ADF watermark: updated_at (incremental)
-- line_total is PERSISTED COMPUTED — not inserted directly

IF NOT EXISTS (SELECT 1 FROM sys.objects WHERE object_id = OBJECT_ID(N'velora_oms.order_lines') AND type = 'U')
BEGIN
    CREATE TABLE velora_oms.order_lines (
        line_id         NVARCHAR(36)    NOT NULL,
        order_id        NVARCHAR(36)    NOT NULL,
        product_id      NVARCHAR(36)    NOT NULL,
        quantity        INT             NOT NULL,
        unit_price      DECIMAL(10,2)   NOT NULL,
        discount_amt    DECIMAL(10,2)   NOT NULL DEFAULT 0,
        line_total      AS (CAST(quantity AS DECIMAL(12,2)) * unit_price - discount_amt) PERSISTED,
        created_at      DATETIME2       NOT NULL DEFAULT GETUTCDATE(),
        updated_at      DATETIME2       NOT NULL DEFAULT GETUTCDATE(),
        source_system   NVARCHAR(20)    NOT NULL DEFAULT 'VELORA_OMS',
        CONSTRAINT PK_order_lines PRIMARY KEY (line_id)
    );
    CREATE INDEX IX_order_lines_order_id   ON velora_oms.order_lines (order_id);
    CREATE INDEX IX_order_lines_product_id ON velora_oms.order_lines (product_id);
    CREATE INDEX IX_order_lines_updated_at ON velora_oms.order_lines (updated_at);
    PRINT 'Created velora_oms.order_lines';
END
GO

-- ── velora_oms.order_status_log ───────────────────────────────────────────────
-- ADF watermark: created_at (append-only log, no updates)

IF NOT EXISTS (SELECT 1 FROM sys.objects WHERE object_id = OBJECT_ID(N'velora_oms.order_status_log') AND type = 'U')
BEGIN
    CREATE TABLE velora_oms.order_status_log (
        log_id          NVARCHAR(36)    NOT NULL,
        order_id        NVARCHAR(36)    NOT NULL,
        from_status     NVARCHAR(30)    NOT NULL,
        to_status       NVARCHAR(30)    NOT NULL,
        changed_at      DATETIME2       NOT NULL,
        changed_by      NVARCHAR(50)    NOT NULL DEFAULT 'SYSTEM',  -- SYSTEM | AGENT | user_id
        created_at      DATETIME2       NOT NULL DEFAULT GETUTCDATE(),
        source_system   NVARCHAR(20)    NOT NULL DEFAULT 'VELORA_OMS',
        CONSTRAINT PK_order_status_log PRIMARY KEY (log_id)
    );
    CREATE INDEX IX_order_status_log_order_id  ON velora_oms.order_status_log (order_id);
    CREATE INDEX IX_order_status_log_created_at ON velora_oms.order_status_log (created_at);
    PRINT 'Created velora_oms.order_status_log';
END
GO

-- ── velora_oms.control_flags ──────────────────────────────────────────────────
-- Internal control table used by failure_injector dependency_violation scenario.
-- ADF checks force_early_fact_run flag before triggering Gold pipeline.

IF NOT EXISTS (SELECT 1 FROM sys.objects WHERE object_id = OBJECT_ID(N'velora_oms.control_flags') AND type = 'U')
BEGIN
    CREATE TABLE velora_oms.control_flags (
        flag_id         INT             NOT NULL IDENTITY(1,1),
        flag_name       NVARCHAR(100)   NOT NULL,
        flag_value      NVARCHAR(100)   NOT NULL,
        set_at          DATETIME2       NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT PK_control_flags PRIMARY KEY (flag_id)
    );
    PRINT 'Created velora_oms.control_flags';
END
GO

-- ── velora_crm.customers ──────────────────────────────────────────────────────
-- ADF watermark: updated_at (incremental)
-- SCD Type 2 tracked in Gold: segment and city fields

IF NOT EXISTS (SELECT 1 FROM sys.objects WHERE object_id = OBJECT_ID(N'velora_crm.customers') AND type = 'U')
BEGIN
    CREATE TABLE velora_crm.customers (
        customer_id     NVARCHAR(36)    NOT NULL,
        full_name       NVARCHAR(200)   NOT NULL,
        email           NVARCHAR(200)   NOT NULL,
        phone           NVARCHAR(20)    NULL,
        segment         NVARCHAR(30)    NOT NULL,   -- INDIVIDUAL | BUSINESS | VIP
        city            NVARCHAR(100)   NOT NULL,
        state           NVARCHAR(100)   NOT NULL,
        account_type    NVARCHAR(20)    NOT NULL,   -- D2C | B2B
        is_active       BIT             NOT NULL DEFAULT 1,
        created_at      DATETIME2       NOT NULL DEFAULT GETUTCDATE(),
        updated_at      DATETIME2       NOT NULL DEFAULT GETUTCDATE(),
        source_system   NVARCHAR(20)    NOT NULL DEFAULT 'VELORA_CRM',
        CONSTRAINT PK_customers PRIMARY KEY (customer_id)
    );
    CREATE INDEX IX_customers_updated_at   ON velora_crm.customers (updated_at);
    CREATE INDEX IX_customers_account_type ON velora_crm.customers (account_type);
    PRINT 'Created velora_crm.customers';
END
GO

-- ── velora_crm.customer_addresses ────────────────────────────────────────────
-- ADF watermark: updated_at (incremental)

IF NOT EXISTS (SELECT 1 FROM sys.objects WHERE object_id = OBJECT_ID(N'velora_crm.customer_addresses') AND type = 'U')
BEGIN
    CREATE TABLE velora_crm.customer_addresses (
        address_id      NVARCHAR(36)    NOT NULL,
        customer_id     NVARCHAR(36)    NOT NULL,
        address_line    NVARCHAR(500)   NOT NULL,
        city            NVARCHAR(100)   NOT NULL,
        state           NVARCHAR(100)   NOT NULL,
        pincode         NVARCHAR(10)    NOT NULL,
        is_primary      BIT             NOT NULL DEFAULT 0,
        address_type    NVARCHAR(20)    NOT NULL,   -- HOME | WORK | BILLING | SHIPPING
        created_at      DATETIME2       NOT NULL DEFAULT GETUTCDATE(),
        updated_at      DATETIME2       NOT NULL DEFAULT GETUTCDATE(),
        source_system   NVARCHAR(20)    NOT NULL DEFAULT 'VELORA_CRM',
        CONSTRAINT PK_customer_addresses PRIMARY KEY (address_id)
    );
    CREATE INDEX IX_customer_addresses_customer_id ON velora_crm.customer_addresses (customer_id);
    CREATE INDEX IX_customer_addresses_updated_at  ON velora_crm.customer_addresses (updated_at);
    PRINT 'Created velora_crm.customer_addresses';
END
GO

-- ── velora_pim.products ───────────────────────────────────────────────────────
-- ADF watermark: updated_at (incremental)
-- SCD Type 2 tracked in Gold on list_price (via product_pricing)

IF NOT EXISTS (SELECT 1 FROM sys.objects WHERE object_id = OBJECT_ID(N'velora_pim.products') AND type = 'U')
BEGIN
    CREATE TABLE velora_pim.products (
        product_id      NVARCHAR(36)    NOT NULL,
        sku             NVARCHAR(50)    NOT NULL,
        product_name    NVARCHAR(500)   NOT NULL,
        category_id     NVARCHAR(36)    NOT NULL,
        division        NVARCHAR(50)    NOT NULL,   -- CONSUMER_ELECTRONICS | HOME_APPLIANCES | PERSONAL_CARE | SPORTS_FITNESS | PREMIUM_ACCESSORIES
        brand           NVARCHAR(100)   NOT NULL,
        is_active       BIT             NOT NULL DEFAULT 1,
        launched_date   DATE            NOT NULL,
        created_at      DATETIME2       NOT NULL DEFAULT GETUTCDATE(),
        updated_at      DATETIME2       NOT NULL DEFAULT GETUTCDATE(),
        source_system   NVARCHAR(20)    NOT NULL DEFAULT 'VELORA_PIM',
        CONSTRAINT PK_products PRIMARY KEY (product_id),
        CONSTRAINT UQ_products_sku UNIQUE (sku)
    );
    CREATE INDEX IX_products_updated_at ON velora_pim.products (updated_at);
    CREATE INDEX IX_products_division   ON velora_pim.products (division);
    CREATE INDEX IX_products_category   ON velora_pim.products (category_id);
    PRINT 'Created velora_pim.products';
END
GO

-- ── velora_pim.product_pricing ────────────────────────────────────────────────
-- ADF watermark: created_at (append-only by design — old records closed, new ones inserted)
-- effective_to = NULL means currently active pricing

IF NOT EXISTS (SELECT 1 FROM sys.objects WHERE object_id = OBJECT_ID(N'velora_pim.product_pricing') AND type = 'U')
BEGIN
    CREATE TABLE velora_pim.product_pricing (
        pricing_id      NVARCHAR(36)    NOT NULL,
        product_id      NVARCHAR(36)    NOT NULL,
        list_price      DECIMAL(10,2)   NOT NULL,
        cost_price      DECIMAL(10,2)   NOT NULL,
        effective_from  DATE            NOT NULL,
        effective_to    DATE            NULL,       -- NULL = currently active
        pricing_type    NVARCHAR(20)    NOT NULL DEFAULT 'STANDARD',  -- STANDARD | PROMOTIONAL | CLEARANCE
        created_at      DATETIME2       NOT NULL DEFAULT GETUTCDATE(),
        source_system   NVARCHAR(20)    NOT NULL DEFAULT 'VELORA_PIM',
        CONSTRAINT PK_product_pricing PRIMARY KEY (pricing_id)
    );
    CREATE INDEX IX_product_pricing_product_id  ON velora_pim.product_pricing (product_id);
    CREATE INDEX IX_product_pricing_created_at  ON velora_pim.product_pricing (created_at);
    CREATE INDEX IX_product_pricing_effective_to ON velora_pim.product_pricing (effective_to);
    PRINT 'Created velora_pim.product_pricing';
END
GO

-- ── velora_pim.inventory_snapshot ────────────────────────────────────────────
-- ADF watermark: snapshot_date (full refresh daily — not incremental)
-- 189,000 rows per day (4,200 products × 45 stores)

IF NOT EXISTS (SELECT 1 FROM sys.objects WHERE object_id = OBJECT_ID(N'velora_pim.inventory_snapshot') AND type = 'U')
BEGIN
    CREATE TABLE velora_pim.inventory_snapshot (
        snapshot_id     NVARCHAR(36)    NOT NULL,
        product_id      NVARCHAR(36)    NOT NULL,
        store_id        NVARCHAR(20)    NOT NULL,
        snapshot_date   DATE            NOT NULL,
        opening_stock   INT             NOT NULL,
        units_sold      INT             NOT NULL DEFAULT 0,
        units_returned  INT             NOT NULL DEFAULT 0,
        closing_stock   INT             NOT NULL,
        stockout_flag   BIT             NOT NULL DEFAULT 0,
        reorder_point   INT             NOT NULL,
        created_at      DATETIME2       NOT NULL DEFAULT GETUTCDATE(),
        source_system   NVARCHAR(20)    NOT NULL DEFAULT 'VELORA_PIM',
        CONSTRAINT PK_inventory_snapshot PRIMARY KEY (snapshot_id)
    );
    CREATE INDEX IX_inventory_snapshot_date       ON velora_pim.inventory_snapshot (snapshot_date);
    CREATE INDEX IX_inventory_snapshot_product    ON velora_pim.inventory_snapshot (product_id);
    CREATE INDEX IX_inventory_snapshot_store      ON velora_pim.inventory_snapshot (store_id);
    PRINT 'Created velora_pim.inventory_snapshot';
END
GO

-- ── velora_hrm.sales_reps ─────────────────────────────────────────────────────
-- ADF watermark: updated_at (incremental)

IF NOT EXISTS (SELECT 1 FROM sys.objects WHERE object_id = OBJECT_ID(N'velora_hrm.sales_reps') AND type = 'U')
BEGIN
    CREATE TABLE velora_hrm.sales_reps (
        rep_id          NVARCHAR(36)    NOT NULL,
        full_name       NVARCHAR(200)   NOT NULL,
        email           NVARCHAR(200)   NOT NULL,
        phone           NVARCHAR(20)    NULL,
        hire_date       DATE            NOT NULL,
        is_active       BIT             NOT NULL DEFAULT 1,
        created_at      DATETIME2       NOT NULL DEFAULT GETUTCDATE(),
        updated_at      DATETIME2       NOT NULL DEFAULT GETUTCDATE(),
        source_system   NVARCHAR(20)    NOT NULL DEFAULT 'VELORA_HRM',
        CONSTRAINT PK_sales_reps PRIMARY KEY (rep_id)
    );
    CREATE INDEX IX_sales_reps_updated_at ON velora_hrm.sales_reps (updated_at);
    PRINT 'Created velora_hrm.sales_reps';
END
GO

-- ── velora_hrm.territory_assignments ─────────────────────────────────────────
-- ADF watermark: created_at (append-only by design — old records closed, new inserted)
-- assigned_to = NULL means currently active assignment
-- SCD Type 2 tracked in Gold: territory_id field

IF NOT EXISTS (SELECT 1 FROM sys.objects WHERE object_id = OBJECT_ID(N'velora_hrm.territory_assignments') AND type = 'U')
BEGIN
    CREATE TABLE velora_hrm.territory_assignments (
        assignment_id   NVARCHAR(36)    NOT NULL,
        rep_id          NVARCHAR(36)    NOT NULL,
        territory_id    NVARCHAR(36)    NOT NULL,
        assigned_from   DATE            NOT NULL,
        assigned_to     DATE            NULL,       -- NULL = currently assigned
        is_current      BIT             NOT NULL DEFAULT 1,
        created_at      DATETIME2       NOT NULL DEFAULT GETUTCDATE(),
        source_system   NVARCHAR(20)    NOT NULL DEFAULT 'VELORA_HRM',
        CONSTRAINT PK_territory_assignments PRIMARY KEY (assignment_id)
    );
    CREATE INDEX IX_territory_assignments_rep_id     ON velora_hrm.territory_assignments (rep_id);
    CREATE INDEX IX_territory_assignments_created_at ON velora_hrm.territory_assignments (created_at);
    CREATE INDEX IX_territory_assignments_is_current ON velora_hrm.territory_assignments (is_current);
    PRINT 'Created velora_hrm.territory_assignments';
END
GO

PRINT 'Bootstrap SQL complete. Run generator to seed catalogue data.';
