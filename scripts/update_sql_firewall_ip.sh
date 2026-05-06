#!/usr/bin/env bash
#
# Upsert an Azure SQL firewall rule to your current public IP, so you can
# connect to velora_oms from your laptop without the dedicated VPN.
#
# Idempotent: run it at the start of any session. If the rule already exists
# its IP is updated in place; if not, it's created. Default rule name is
# MG-Office-Laptop-Dynamic — separate from the VPN rule (69.5.168.130) so
# both paths keep working.
#
# Override defaults via env vars:
#   AZURE_SQL_SERVER_NAME    (default: pipelineiq-sql-velora-dev)
#   AZURE_SQL_FIREWALL_RULE  (default: MG-Office-Laptop-Dynamic)
#   AZURE_SUBSCRIPTION       (default: Microsoft Azure Sponsorship)
#
# Usage:
#   bash scripts/update_sql_firewall_ip.sh

set -euo pipefail

SERVER="${AZURE_SQL_SERVER_NAME:-pipelineiq-sql-velora-dev}"
RULE_NAME="${AZURE_SQL_FIREWALL_RULE:-MG-Office-Laptop-Dynamic}"
SUBSCRIPTION="${AZURE_SUBSCRIPTION:-Microsoft Azure Sponsorship}"

echo "Subscription : ${SUBSCRIPTION}"
echo "Server       : ${SERVER}"
echo "Rule name    : ${RULE_NAME}"

# 1. Make sure az is authenticated
if ! az account show >/dev/null 2>&1; then
  echo "ERROR: not logged in to az. Run: az login" >&2
  exit 1
fi

# 2. Switch to the right subscription
az account set --subscription "${SUBSCRIPTION}"

# 3. Detect current public IP (try ipify first, fall back to ifconfig.me)
IP="$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || true)"
if [[ -z "${IP}" ]]; then
  IP="$(curl -fsS --max-time 10 https://ifconfig.me 2>/dev/null || true)"
fi
if [[ -z "${IP}" ]]; then
  echo "ERROR: could not detect public IP from ipify or ifconfig.me" >&2
  exit 1
fi

# Sanity-check the IP shape (IPv4 only — Azure firewall doesn't accept IPv6 here)
if ! [[ "${IP}" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
  echo "ERROR: detected IP '${IP}' is not a valid IPv4 address" >&2
  echo "       (Azure SQL firewall rules require IPv4. Are you on an IPv6-only network?)" >&2
  exit 1
fi
echo "Public IP    : ${IP}"

# 4. Discover the resource group from the server name (no hardcoding)
RG="$(az sql server list \
        --query "[?name=='${SERVER}'].resourceGroup | [0]" \
        -o tsv)"
if [[ -z "${RG}" || "${RG}" == "null" ]]; then
  echo "ERROR: SQL server '${SERVER}' not found in subscription '${SUBSCRIPTION}'" >&2
  exit 1
fi
echo "Resource grp : ${RG}"
echo

# 5. Upsert the rule
if az sql server firewall-rule show \
     -g "${RG}" -s "${SERVER}" -n "${RULE_NAME}" \
     >/dev/null 2>&1; then
  CURRENT_IP="$(az sql server firewall-rule show \
                  -g "${RG}" -s "${SERVER}" -n "${RULE_NAME}" \
                  --query startIpAddress -o tsv)"
  if [[ "${CURRENT_IP}" == "${IP}" ]]; then
    echo "Rule already points at ${IP} — nothing to do."
    exit 0
  fi
  echo "Updating ${RULE_NAME}: ${CURRENT_IP} → ${IP}"
  az sql server firewall-rule update \
    -g "${RG}" -s "${SERVER}" -n "${RULE_NAME}" \
    --start-ip-address "${IP}" --end-ip-address "${IP}" \
    -o table
else
  echo "Creating ${RULE_NAME} = ${IP}"
  az sql server firewall-rule create \
    -g "${RG}" -s "${SERVER}" -n "${RULE_NAME}" \
    --start-ip-address "${IP}" --end-ip-address "${IP}" \
    -o table
fi

echo
echo "Done. ${SERVER} now accepts connections from ${IP}."
