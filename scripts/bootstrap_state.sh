#!/usr/bin/env bash
# PipelineIQ — Terraform Remote State Bootstrap
#
# Creates the Azure Storage Account and container that Terraform uses as
# its remote state backend. Must be run ONCE before any terraform command.
#
# Prerequisites:
#   - Azure CLI installed and logged in (az login)
#   - Correct subscription selected (az account set --subscription $AZURE_SUBSCRIPTION_ID)
#   - AZURE_SUBSCRIPTION_ID, AZURE_RESOURCE_GROUP, AZURE_TENANT_ID set in environment
#
# Usage:
#   export AZURE_SUBSCRIPTION_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
#   export AZURE_RESOURCE_GROUP=pipelineiq-rg-dev
#   bash scripts/bootstrap_state.sh

set -euo pipefail

RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:-pipelineiq-rg-dev}"
LOCATION="centralindia"
STORAGE_ACCOUNT="pipelineiqtfstate"   # must be globally unique, 3-24 chars, lowercase
CONTAINER_NAME="tfstate"

echo "==> Creating resource group: $RESOURCE_GROUP"
az group create \
  --name "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --tags project=pipelineiq environment=dev owner=data-engineering managed_by=manual

echo "==> Creating Terraform state storage account: $STORAGE_ACCOUNT"
az storage account create \
  --name "$STORAGE_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --sku Standard_LRS \
  --kind StorageV2 \
  --access-tier Hot \
  --min-tls-version TLS1_2 \
  --allow-blob-public-access false \
  --tags project=pipelineiq environment=dev owner=data-engineering managed_by=manual

echo "==> Creating tfstate container"
az storage container create \
  --name "$CONTAINER_NAME" \
  --account-name "$STORAGE_ACCOUNT" \
  --auth-mode login

echo ""
echo "==> Terraform state backend ready."
echo "    Add this backend block to pipelineiq-iac/clients/velora/main.tf:"
echo ""
echo "    terraform {"
echo "      backend \"azurerm\" {"
echo "        resource_group_name  = \"$RESOURCE_GROUP\""
echo "        storage_account_name = \"$STORAGE_ACCOUNT\""
echo "        container_name       = \"$CONTAINER_NAME\""
echo "        key                  = \"velora.tfstate\""
echo "      }"
echo "    }"
echo ""
echo "==> Then run: terraform init"
