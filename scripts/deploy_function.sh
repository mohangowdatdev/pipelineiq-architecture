#!/usr/bin/env bash
# Deploy the PipelineIQ daily-generator Function to Azure.
#
# Builds a deployment ZIP with:
#   - host.json at root (moved from generator/host.json)
#   - requirements.txt at root (slim — only generator deps)
#   - function_app.py at root (V2 model: generator timer + 5 control-plane endpoints)
#   - generator/ directory with main.py + all generator modules (V1 function.json
#     removed in S17 — dead under the V2 host)
#
# Then ZIP-deploys via `az functionapp deployment source config-zip`.
# Azure Oryx builds the Python deps server-side (SCM_DO_BUILD_DURING_DEPLOYMENT=true).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FUNCTION_APP="${FUNCTION_APP:-pipelineiq-functions-dev}"
RESOURCE_GROUP="${RESOURCE_GROUP:-pipelineiq-rg-dev}"
SUBSCRIPTION="${SUBSCRIPTION:-ea05f17f-b2bb-40ac-a391-afe41a9f5cbf}"

BUILD_DIR="$(mktemp -d /tmp/pipelineiq-func-build.XXXXXX)"
trap 'rm -rf "$BUILD_DIR"' EXIT

ZIP_PATH="$BUILD_DIR/function.zip"
STAGE_DIR="$BUILD_DIR/stage"
mkdir -p "$STAGE_DIR/generator"

echo "Staging deployment artifact at $STAGE_DIR"

# host.json at deploy root
cp "$REPO_ROOT/generator/host.json" "$STAGE_DIR/host.json"

# Slim requirements.txt — only what the timer-triggered generator needs.
# Keeps cold-start small and avoids pulling pyarrow / azure-storage-file-datalake
# / databricks-sdk into the Function (those are for the laptop tooling only).
cat > "$STAGE_DIR/requirements.txt" <<'PY'
azure-functions>=1.18.0
azure-identity>=1.15.0
azure-monitor-opentelemetry>=1.6.0
pyodbc>=5.0.0
psycopg[binary]>=3.1
psycopg-pool>=3.2
numpy>=1.26.0
pandas>=2.2.0
faker>=24.0.0
python-dotenv>=1.0.0
PY

# Copy the generator package contents (function.json + main.py + all modules)
# Exclude __pycache__ + host.json (already at root).
rsync -a \
  --exclude='__pycache__' \
  --exclude='host.json' \
  --exclude='*.pyc' \
  --exclude='requirements.txt' \
  "$REPO_ROOT/generator/" "$STAGE_DIR/generator/"

# V2 model: function_app.py at deploy root for the control-plane HTTP endpoints.
# Coexists with the V1 generator/ timer function.
cp "$REPO_ROOT/functions/function_app.py" "$STAGE_DIR/function_app.py"

echo "Stage contents:"
(cd "$STAGE_DIR" && find . -type f | sort)

# Zip from inside the stage dir so paths are relative to the deploy root.
(cd "$STAGE_DIR" && zip -r "$ZIP_PATH" . > /dev/null)
echo "Built ZIP: $ZIP_PATH ($(du -h "$ZIP_PATH" | cut -f1))"

echo
echo "Deploying to $FUNCTION_APP ..."
az functionapp deployment source config-zip \
  --subscription "$SUBSCRIPTION" \
  --resource-group "$RESOURCE_GROUP" \
  --name "$FUNCTION_APP" \
  --src "$ZIP_PATH" \
  --build-remote true \
  --verbose

echo
echo "Deployment complete. Tail logs with:"
echo "  az webapp log tail --subscription $SUBSCRIPTION -g $RESOURCE_GROUP -n $FUNCTION_APP"
