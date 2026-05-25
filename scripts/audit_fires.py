"""
Phase A auto-fire audit: query velora_oms for orders / order_status_log /
inventory_snapshot row counts per date in a window, plus first-insert UTC
timestamp (proxy for autonomous-fire landing time).

Read-only. AAD-token auth via DefaultAzureCredential (az login on laptop).
Retries 40613 (SQL serverless cold-start) up to 12 times with backoff.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import date

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "generator"))

os.environ["AZURE_SQL_AUTH_MODE"] = "aad"
import config  # noqa: E402
import pyodbc  # noqa: E402

START = date(2026, 5, 12)
END = date(2026, 5, 24)


def connect_with_retry():
    delay = 10
    for attempt in range(1, 13):
        try:
            return config.connect_aad(timeout=120)
        except (pyodbc.OperationalError, pyodbc.Error) as e:
            msg = str(e)
            if "40613" in msg or "HYT00" in msg or "is not currently available" in msg:
                print(f"[attempt {attempt}] cold-start: sleeping {delay}s …")
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            raise
    raise RuntimeError("Failed to wake Azure SQL after 12 attempts")


def main() -> None:
    conn = connect_with_retry()
    cur = conn.cursor()

    print(f"\n== orders by order_date ({START} → {END}) ==")
    cur.execute(
        """
        SELECT CAST(order_date AS DATE) AS d,
               COUNT(*) AS n,
               MIN(created_at) AS first_insert_utc,
               MAX(created_at) AS last_insert_utc
        FROM velora_oms.orders
        WHERE order_date BETWEEN ? AND ?
        GROUP BY CAST(order_date AS DATE)
        ORDER BY d
        """,
        START, END,
    )
    print(f"{'date':<12} {'orders':>8} {'first_insert_utc':<28} {'last_insert_utc':<28}")
    for d, n, fi, li in cur.fetchall():
        print(f"{str(d):<12} {n:>8} {str(fi):<28} {str(li):<28}")

    print(f"\n== inventory_snapshot by snapshot_date ({START} → {END}) ==")
    cur.execute(
        """
        SELECT CAST(snapshot_date AS DATE) AS d,
               COUNT(*) AS n,
               MIN(created_at) AS first_insert_utc,
               MAX(created_at) AS last_insert_utc
        FROM velora_pim.inventory_snapshot
        WHERE snapshot_date BETWEEN ? AND ?
        GROUP BY CAST(snapshot_date AS DATE)
        ORDER BY d
        """,
        START, END,
    )
    print(f"{'date':<12} {'rows':>10} {'first_insert_utc':<28} {'last_insert_utc':<28}")
    for d, n, fi, li in cur.fetchall():
        flag = "" if n == 189225 else "  <-- PARTIAL" if n > 0 else "  <-- MISSING"
        print(f"{str(d):<12} {n:>10} {str(fi):<28} {str(li):<28}{flag}")

    print(f"\n== order_status_log by created_at date ({START} → {END}) ==")
    cur.execute(
        """
        SELECT CAST(created_at AS DATE) AS d,
               COUNT(*) AS n,
               MIN(created_at) AS first_insert_utc
        FROM velora_oms.order_status_log
        WHERE CAST(created_at AS DATE) BETWEEN ? AND ?
        GROUP BY CAST(created_at AS DATE)
        ORDER BY d
        """,
        START, END,
    )
    print(f"{'date':<12} {'rows':>8} {'first_insert_utc':<28}")
    for d, n, fi in cur.fetchall():
        print(f"{str(d):<12} {n:>8} {str(fi):<28}")

    print("\n== order_lines by order_date ==")
    cur.execute(
        """
        SELECT CAST(o.order_date AS DATE) AS d, COUNT(*) AS n
        FROM velora_oms.order_lines l
        JOIN velora_oms.orders o ON o.order_id = l.order_id
        WHERE o.order_date BETWEEN ? AND ?
        GROUP BY CAST(o.order_date AS DATE)
        ORDER BY d
        """,
        START, END,
    )
    print(f"{'date':<12} {'lines':>8}")
    for d, n in cur.fetchall():
        print(f"{str(d):<12} {n:>8}")

    print("\n== sanity: last 3 status-log rows ==")
    cur.execute(
        "SELECT TOP 3 log_id, order_id, from_status, to_status, changed_at, created_at "
        "FROM velora_oms.order_status_log ORDER BY created_at DESC"
    )
    for row in cur.fetchall():
        print(row)

    conn.close()


if __name__ == "__main__":
    main()
