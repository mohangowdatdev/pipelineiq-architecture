"""One-shot runner for bootstrap_sql.sql against Azure SQL using AAD token auth.

Run: `.venv/bin/python scripts/run_bootstrap_sql.py`

Reuses the current `az login` identity via DefaultAzureCredential. Splits the
script on `GO` batches (the T-SQL batch separator, which pyodbc cannot execute
as one blob because each CREATE SCHEMA must be in its own batch).
"""

from pathlib import Path
import struct
import sys

import pyodbc
from azure.identity import DefaultAzureCredential

SERVER = "pipelineiq-sql-velora-dev.database.windows.net"
DATABASE = "velora_oms"
SQL_FILE = Path(__file__).parent / "bootstrap_sql.sql"
SQL_COPT_SS_ACCESS_TOKEN = 1256  # ODBC connection attribute for AAD token


def get_token_struct() -> bytes:
    cred = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    token = cred.get_token("https://database.windows.net/.default").token
    token_bytes = token.encode("utf-16-le")
    return struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)


def split_batches(sql: str) -> list[str]:
    batches: list[str] = []
    current: list[str] = []
    for line in sql.splitlines():
        if line.strip().upper() == "GO":
            if current:
                batches.append("\n".join(current))
                current = []
        else:
            current.append(line)
    if current:
        batches.append("\n".join(current))
    return [b for b in batches if b.strip()]


def main() -> int:
    conn_str = (
        f"DRIVER={{ODBC Driver 18 for SQL Server}};"
        f"SERVER={SERVER};"
        f"DATABASE={DATABASE};"
        f"Encrypt=yes;TrustServerCertificate=no;"
    )
    token_struct = get_token_struct()

    sql = SQL_FILE.read_text()
    batches = split_batches(sql)
    print(f"Loaded {SQL_FILE.name}: {len(batches)} batches")

    with pyodbc.connect(conn_str, attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token_struct}) as conn:
        conn.autocommit = True
        cur = conn.cursor()
        for i, batch in enumerate(batches, 1):
            try:
                cur.execute(batch)
                print(f"  [{i}/{len(batches)}] OK")
            except pyodbc.Error as e:
                print(f"  [{i}/{len(batches)}] FAILED: {e}")
                print(f"  Batch preview: {batch[:120].strip()}...")
                return 1

    print("\nBootstrap SQL complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
