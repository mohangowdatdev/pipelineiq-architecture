"""Smoke-test the first Bronze notebook against the live workspace.

Steps:
1. Upload `notebooks/bronze/ingest_to_bronze.py` into the workspace.
2. Submit a one-time job that runs it for one entity (default: customers).
3. Poll until the run finishes; print result + any error.

Auth: AAD via DefaultAzureCredential — uses your `az login` session.
The Databricks SDK auto-detects the workspace URL and the principal.

Usage:
    .venv/bin/python scripts/run_bronze_smoke.py
    .venv/bin/python scripts/run_bronze_smoke.py --entity orders
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.compute import DataSecurityMode
from databricks.sdk.service import jobs, compute
from databricks.sdk.service.workspace import ImportFormat, Language

WORKSPACE_HOST = "https://adb-7405617631498102.2.azuredatabricks.net"
LOCAL_NOTEBOOK = (
    Path(__file__).resolve().parent.parent
    / "notebooks/bronze/ingest_to_bronze.py"
)
WS_NOTEBOOK_PATH = "/Shared/pipelineiq/bronze/ingest_to_bronze"
CLUSTER_POLICY_ID = "000E52A43E9F9628"


def upload_notebook(w: WorkspaceClient) -> None:
    print(f"Uploading {LOCAL_NOTEBOOK.name} → {WS_NOTEBOOK_PATH}")
    parent = WS_NOTEBOOK_PATH.rsplit("/", 1)[0]
    w.workspace.mkdirs(parent)
    with LOCAL_NOTEBOOK.open("rb") as f:
        content = f.read()
    w.workspace.upload(
        path=WS_NOTEBOOK_PATH,
        content=content,
        format=ImportFormat.SOURCE,
        language=Language.PYTHON,
        overwrite=True,
    )
    print(f"  uploaded ({len(content):,} bytes)")


def submit_run(w: WorkspaceClient, entity: str) -> int:
    pipeline_run_id = str(uuid.uuid4())
    print(f"\nSubmitting one-time run: entity={entity} pipeline_run_id={pipeline_run_id}")

    run = w.jobs.submit(
        run_name=f"bronze-smoke-{entity}",
        tasks=[
            jobs.SubmitTask(
                task_key="bronze",
                notebook_task=jobs.NotebookTask(
                    notebook_path=WS_NOTEBOOK_PATH,
                    base_parameters={
                        "entity_name": entity,
                        "pipeline_run_id": pipeline_run_id,
                    },
                ),
                new_cluster=compute.ClusterSpec(
                    spark_version="14.3.x-scala2.12",
                    node_type_id="Standard_DS3_v2",
                    num_workers=1,
                    data_security_mode=DataSecurityMode.SINGLE_USER,
                ),
            ),
        ],
    )
    run_id = run.run_id
    print(f"  run_id = {run_id}")
    return run_id


def wait_for_run(w: WorkspaceClient, run_id: int) -> None:
    print(f"\nWaiting on run {run_id} ...")
    while True:
        r = w.jobs.get_run(run_id)
        state = r.state
        life_cycle = state.life_cycle_state.value if state and state.life_cycle_state else "?"
        result = state.result_state.value if state and state.result_state else None
        print(f"  state: {life_cycle}{f' / {result}' if result else ''}")
        if life_cycle in ("TERMINATED", "INTERNAL_ERROR", "SKIPPED"):
            break
        time.sleep(15)

    if r.state.result_state and r.state.result_state.value != "SUCCESS":
        print(f"\nFAILED: {r.state.state_message}")
        # Print tail of run output if available
        try:
            task_run_id = r.tasks[0].run_id
            output = w.jobs.get_run_output(task_run_id)
            if output.error:
                print(f"\nError:\n{output.error}")
            if output.error_trace:
                print(f"\nTrace:\n{output.error_trace[:2000]}")
        except Exception as e:
            print(f"  (could not fetch detailed error: {e})")
        sys.exit(1)

    print("\nRun complete.")
    try:
        task_run_id = r.tasks[0].run_id
        output = w.jobs.get_run_output(task_run_id)
        if output.notebook_output and output.notebook_output.result:
            print(f"\nNotebook output:\n{output.notebook_output.result}")
        if output.logs:
            print(f"\nLogs (tail):\n{output.logs[-2000:]}")
    except Exception as e:
        print(f"  (could not fetch run output: {e})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--entity", default="customers", help="Entity to ingest (default: customers — small, fast).")
    args = ap.parse_args()

    w = WorkspaceClient(host=WORKSPACE_HOST, auth_type="azure-cli")

    upload_notebook(w)
    run_id = submit_run(w, args.entity)
    wait_for_run(w, run_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
