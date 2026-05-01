"""One-shot Azure SQL → ADLS landing/ Parquet exporter.

Substitutes for the eventual ADF Copy Activity (Tier 6, deferred). Exists so
Bronze notebook development can proceed without ADF infra. AAD auth via
DefaultAzureCredential — `az login` must be active for the Sponsorship
subscription.

Two extraction modes per entity:

  by_date:  partition by a business-date column (e.g. orders.order_date,
            inventory_snapshot.snapshot_date). One Parquet per (entity, date).
            Path: landing/{entity}/date=YYYY-MM-DD/{entity}_{run_id}.parquet

  full:     dump the entire table once per pipeline run. Master / dimension
            tables that have no logical day partition.
            Path: landing/{entity}/full/{entity}_{run_id}.parquet

Note: pandas.read_sql doesn't take a raw pyodbc connection cleanly anymore,
so we use cursor.execute + cursor.fetchall and build the DataFrame manually.

Usage:
    .venv/bin/python scripts/export_velora_to_landing.py --date 2026-01-15
    .venv/bin/python scripts/export_velora_to_landing.py --start 2026-01-15 --end 2026-01-21
    .venv/bin/python scripts/export_velora_to_landing.py --date 2026-01-15 --entity orders

Requires: pyodbc, pandas, pyarrow, azure-identity, azure-storage-file-datalake.
"""

from __future__ import annotations

import argparse
import io
import struct
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pyodbc
from azure.identity import DefaultAzureCredential
from azure.storage.filedatalake import DataLakeServiceClient

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------

SQL_SERVER = "pipelineiq-sql-velora-dev.database.windows.net"
SQL_DATABASE = "velora_oms"
ADLS_ACCOUNT = "pipelineiqadlsdev"
LANDING_FS = "landing"

SQL_COPT_SS_ACCESS_TOKEN = 1256  # ODBC: pre-auth via AAD bearer token

# (schema, table, mode, business_col, business_col_kind)
# Static reference tables (product_categories, stores, control_flags) are
# excluded — they are loaded directly into Gold from the seed (per SCHEMA.md).
#
# Why business-date columns rather than created_at/updated_at: the generator
# left audit columns at the GETUTCDATE() default — they record seed time,
# not the logical run date. ADF in production will drive incremental loads
# off updated_at; for this one-shot scaffold we pivot on business semantics.
ENTITIES: list[tuple[str, str, str, str | None, str | None]] = [
    ("velora_oms", "orders", "by_date", "order_date", "date"),
    ("velora_oms", "order_lines", "full", None, None),
    ("velora_oms", "order_status_log", "full", None, None),
    ("velora_crm", "customers", "full", None, None),
    ("velora_crm", "customer_addresses", "full", None, None),
    ("velora_pim", "products", "full", None, None),
    ("velora_pim", "product_pricing", "full", None, None),
    ("velora_pim", "inventory_snapshot", "by_date", "snapshot_date", "date"),
    ("velora_hrm", "sales_reps", "full", None, None),
    ("velora_hrm", "territory_assignments", "full", None, None),
]

FETCH_CHUNK = 5000  # rows per cursor.fetchmany call
MAX_RETRIES = 4
RETRY_BACKOFF_S = (5, 15, 30, 60)


# ------------------------------------------------------------
# Auth helpers
# ------------------------------------------------------------


def sql_token_struct(cred: DefaultAzureCredential) -> bytes:
    token = cred.get_token("https://database.windows.net/.default").token
    token_bytes = token.encode("utf-16-le")
    return struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)


def sql_connect(cred: DefaultAzureCredential) -> pyodbc.Connection:
    conn_str = (
        f"DRIVER={{ODBC Driver 18 for SQL Server}};"
        f"SERVER={SQL_SERVER};"
        f"DATABASE={SQL_DATABASE};"
        f"Encrypt=yes;TrustServerCertificate=no;"
        f"Connection Timeout=90;"
    )
    return pyodbc.connect(
        conn_str,
        attrs_before={SQL_COPT_SS_ACCESS_TOKEN: sql_token_struct(cred)},
    )


# ------------------------------------------------------------
# Read + write
# ------------------------------------------------------------


def fetch_df_chunked(cursor: pyodbc.Cursor, sql: str, params: tuple = ()) -> pd.DataFrame:
    """Execute and fetchmany in chunks. Returns one DataFrame.

    Chunked fetchmany reduces the chance of TCP reset on long reads
    (Azure SQL serverless + VPN tends to drop ~3 min single-cursor fetches).
    """
    if params:
        cursor.execute(sql, *params)
    else:
        cursor.execute(sql)
    cols = [c[0] for c in cursor.description]
    chunks: list[list[tuple]] = []
    while True:
        batch = cursor.fetchmany(FETCH_CHUNK)
        if not batch:
            break
        chunks.append([tuple(r) for r in batch])
    if not chunks:
        return pd.DataFrame(columns=cols)
    flat = [row for batch in chunks for row in batch]
    return pd.DataFrame.from_records(flat, columns=cols)


def fetch_df_with_retry(
    sql_connect_fn,
    sql: str,
    params: tuple = (),
) -> pd.DataFrame:
    """Open a fresh connection, fetch, close. Retry on transient errors."""
    import time
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            with sql_connect_fn() as conn:
                cur = conn.cursor()
                return fetch_df_chunked(cur, sql, params)
        except (pyodbc.OperationalError, pyodbc.Error) as e:
            last_err = e
            if attempt == MAX_RETRIES:
                break
            wait = RETRY_BACKOFF_S[attempt]
            print(f"    transient error: {str(e)[:120]} — retry in {wait}s")
            time.sleep(wait)
    assert last_err is not None
    raise last_err


def read_by_date(
    sql_connect_fn,
    schema: str,
    table: str,
    business_col: str,
    business_col_kind: str,
    run_date: date,
) -> pd.DataFrame:
    if business_col_kind == "date":
        sql = f"SELECT * FROM {schema}.{table} WHERE {business_col} = ?"
        return fetch_df_with_retry(sql_connect_fn, sql, (run_date,))

    start = datetime.combine(run_date, datetime.min.time())
    end = start + timedelta(days=1)
    sql = (
        f"SELECT * FROM {schema}.{table} "
        f"WHERE {business_col} >= ? AND {business_col} < ?"
    )
    return fetch_df_with_retry(sql_connect_fn, sql, (start, end))


def read_full(sql_connect_fn, schema: str, table: str) -> pd.DataFrame:
    return fetch_df_with_retry(sql_connect_fn, f"SELECT * FROM {schema}.{table}")


def write_parquet_to_adls(
    df: pd.DataFrame,
    fs_client,
    rel_path: str,
    pipeline_run_id: str,
    entity: str,
    partition_label: str,
) -> None:
    table = pa.Table.from_pandas(df, preserve_index=False)
    metadata = {
        b"_pipeline_run_id": pipeline_run_id.encode(),
        b"_extract_timestamp": datetime.now(timezone.utc).isoformat().encode(),
        b"_source_entity": entity.encode(),
        b"_partition": partition_label.encode(),
    }
    existing = table.schema.metadata or {}
    table = table.replace_schema_metadata({**existing, **metadata})

    buf = io.BytesIO()
    pq.write_table(table, buf, compression="snappy")
    fs_client.get_file_client(rel_path).upload_data(
        buf.getvalue(), overwrite=True
    )


# ------------------------------------------------------------
# Driver
# ------------------------------------------------------------


def daterange(start: date, end: date) -> Iterable[date]:
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--date", help="Single date YYYY-MM-DD")
    g.add_argument("--start", help="Range start YYYY-MM-DD (with --end)")
    p.add_argument("--end", help="Range end YYYY-MM-DD (inclusive)")
    p.add_argument(
        "--entity",
        help="Restrict to one entity (table name without schema). Default: all.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.date:
        dates = [date.fromisoformat(args.date)]
    else:
        if not args.end:
            print("--start requires --end", file=sys.stderr)
            return 2
        dates = list(
            daterange(date.fromisoformat(args.start), date.fromisoformat(args.end))
        )

    entities = ENTITIES
    if args.entity:
        entities = [e for e in ENTITIES if e[1] == args.entity]
        if not entities:
            print(f"Unknown entity: {args.entity}", file=sys.stderr)
            return 2

    by_date_entities = [e for e in entities if e[2] == "by_date"]
    full_entities = [e for e in entities if e[2] == "full"]

    cred = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    service = DataLakeServiceClient(
        account_url=f"https://{ADLS_ACCOUNT}.dfs.core.windows.net",
        credential=cred,
    )
    fs_client = service.get_file_system_client(LANDING_FS)

    pipeline_run_id = str(uuid.uuid4())
    print(f"pipeline_run_id = {pipeline_run_id}")
    print(f"dates: {[d.isoformat() for d in dates]}")
    print(f"by_date entities: {[e[1] for e in by_date_entities]}")
    print(f"full entities: {[e[1] for e in full_entities]}\n")

    # Each read opens a fresh connection. Heavier than reusing one cursor,
    # but resilient to TCP resets on long fetches over VPN — the same fix
    # pattern as scripts/backfill_inventory.py per Session 4.
    def open_conn() -> pyodbc.Connection:
        return sql_connect(cred)

    total_rows = 0

    for run_date in dates:
        for schema, table, _mode, business_col, business_kind in by_date_entities:
            df = read_by_date(
                open_conn, schema, table, business_col, business_kind, run_date
            )
            if df.empty:
                print(f"  [{run_date}] {schema}.{table}: 0 rows (skipped)")
                continue
            run_id = uuid.uuid4().hex[:12]
            rel = (
                f"{table}/date={run_date.isoformat()}"
                f"/{table}_{run_id}.parquet"
            )
            write_parquet_to_adls(
                df, fs_client, rel, pipeline_run_id, table,
                partition_label=run_date.isoformat(),
            )
            print(
                f"  [{run_date}] {schema}.{table}: "
                f"{len(df):>7} rows → landing/{rel}"
            )
            total_rows += len(df)

    # Full-dump entities — one snapshot per run, regardless of date count.
    for schema, table, _mode, _bc, _bk in full_entities:
        df = read_full(open_conn, schema, table)
        if df.empty:
            print(f"  [full] {schema}.{table}: 0 rows (skipped)")
            continue
        run_id = uuid.uuid4().hex[:12]
        rel = f"{table}/full/{table}_{run_id}.parquet"
        write_parquet_to_adls(
            df, fs_client, rel, pipeline_run_id, table, partition_label="full"
        )
        print(
            f"  [full]      {schema}.{table}: "
            f"{len(df):>7} rows → landing/{rel}"
        )
        total_rows += len(df)

    print(f"\nDone. {total_rows:,} rows exported.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
