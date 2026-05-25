#!/usr/bin/env bash
# Recover inventory_snapshot for a range of dates after Flex worker-kill
# left partial / zero rows. Uses AAD auth (laptop az login).
#
# Usage:
#   bash scripts/recover_inventory_batch.sh 2026-05-14 2026-05-24

set -euo pipefail

START="${1:?start date YYYY-MM-DD required}"
END="${2:?end date YYYY-MM-DD required}"

export AZURE_SQL_SERVER="pipelineiq-sql-velora-dev.database.windows.net"
export AZURE_SQL_DATABASE="velora_oms"
export AZURE_SQL_AUTH_MODE="aad"

d="$START"
while [[ "$d" < "$END" || "$d" == "$END" ]]; do
  echo ""
  echo "================================================================"
  echo "Recovering inventory for $d"
  echo "================================================================"
  .venv/bin/python -u scripts/inventory_only.py --date "$d" --force
  d=$(date -j -v+1d -f "%Y-%m-%d" "$d" "+%Y-%m-%d")
done

echo ""
echo "All dates recovered $START → $END"
