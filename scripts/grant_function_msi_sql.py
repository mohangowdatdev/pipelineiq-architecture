"""Grant the Function App's managed identity access to velora_oms.

CREATE USER [<function-app-name>] FROM EXTERNAL PROVIDER + role grants.
Run once after the Function App is provisioned. Idempotent — safe to re-run.

Auth: AAD via DefaultAzureCredential (the running user must be the AAD admin
on the Azure SQL server). `az login` with the Sponsorship subscription active.

Usage:
    .venv/bin/python scripts/grant_function_msi_sql.py
    .venv/bin/python scripts/grant_function_msi_sql.py --function-app pipelineiq-functions-dev
"""

from __future__ import annotations

import argparse
import struct
import sys

import pyodbc
from azure.identity import DefaultAzureCredential

SQL_SERVER = "pipelineiq-sql-velora-dev.database.windows.net"
SQL_DATABASE = "velora_oms"
DEFAULT_FUNCTION_APP = "pipelineiq-functions-dev"
SQL_COPT_SS_ACCESS_TOKEN = 1256


def sql_token_struct(cred: DefaultAzureCredential) -> bytes:
    token = cred.get_token("https://database.windows.net/.default").token
    token_bytes = token.encode("utf-16-le")
    return struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--function-app", default=DEFAULT_FUNCTION_APP)
    args = ap.parse_args()

    fn_name = args.function_app

    cred = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    conn_str = (
        f"DRIVER={{ODBC Driver 18 for SQL Server}};"
        f"SERVER={SQL_SERVER};DATABASE={SQL_DATABASE};"
        f"Encrypt=yes;TrustServerCertificate=no;Connection Timeout=90;"
    )

    # Each statement runs in its own batch (T-SQL CREATE USER must be the
    # only statement in its batch in some dialects).
    statements = [
        (
            "create user",
            f"""
            IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'{fn_name}')
            BEGIN
                CREATE USER [{fn_name}] FROM EXTERNAL PROVIDER;
            END
            """,
        ),
        (
            "datareader",
            f"ALTER ROLE db_datareader ADD MEMBER [{fn_name}];",
        ),
        (
            "datawriter",
            f"ALTER ROLE db_datawriter ADD MEMBER [{fn_name}];",
        ),
        (
            "ddladmin",
            f"ALTER ROLE db_ddladmin ADD MEMBER [{fn_name}];",
        ),
    ]

    print(f"Granting velora_oms access to MSI [{fn_name}] ...")
    with pyodbc_connect(conn_str, cred) as conn:
        conn.autocommit = True
        cur = conn.cursor()
        for label, sql in statements:
            try:
                cur.execute(sql)
                print(f"  [{label}] OK")
            except pyodbc.Error as e:
                # Role-add is idempotent-friendly: "is already a member" is fine.
                msg = str(e)
                if "already a member" in msg or "is already" in msg:
                    print(f"  [{label}] already-applied (skipped)")
                else:
                    print(f"  [{label}] FAILED: {msg}")
                    return 1

    print("\nGrant complete.")
    return 0


def pyodbc_connect(conn_str: str, cred: DefaultAzureCredential) -> pyodbc.Connection:
    return pyodbc.connect(
        conn_str,
        attrs_before={SQL_COPT_SS_ACCESS_TOKEN: sql_token_struct(cred)},
    )


if __name__ == "__main__":
    sys.exit(main())
