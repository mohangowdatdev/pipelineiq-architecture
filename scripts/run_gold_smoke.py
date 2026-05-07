"""Smoke-test a Gold notebook against the live workspace.

Usage:
    .venv/bin/python scripts/run_gold_smoke.py                    # default entity=dim_customer
    .venv/bin/python scripts/run_gold_smoke.py --entity dim_customer
    .venv/bin/python scripts/run_gold_smoke.py --entity fact_order_line
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
LOCAL_NOTEBOOK_DIR = Path(__file__).resolve().parent.parent / "notebooks/gold"
WS_NOTEBOOK_DIR = "/Shared/pipelineiq/gold"


def upload_notebook(w: WorkspaceClient, entity: str) -> str:
    local = LOCAL_NOTEBOOK_DIR / f"build_gold_{entity}.py"
    if not local.exists():
        sys.exit(f"Local notebook not found: {local}")

    ws_path = f"{WS_NOTEBOOK_DIR}/build_gold_{entity}"
    print(f"Uploading {local.name} → {ws_path}")
    w.workspace.mkdirs(WS_NOTEBOOK_DIR)
    with local.open("rb") as f:
        content = f.read()
    w.workspace.upload(
        path=ws_path,
        content=content,
        format=ImportFormat.SOURCE,
        language=Language.PYTHON,
        overwrite=True,
    )
    print(f"  uploaded ({len(content):,} bytes)")
    return ws_path


def submit_run(w: WorkspaceClient, entity: str, ws_path: str) -> int:
    pipeline_run_id = str(uuid.uuid4())
    print(f"\nSubmitting one-time run: entity={entity} pipeline_run_id={pipeline_run_id}")

    run = w.jobs.submit(
        run_name=f"gold-smoke-{entity}",
        tasks=[
            jobs.SubmitTask(
                task_key="gold",
                notebook_task=jobs.NotebookTask(
                    notebook_path=ws_path,
                    base_parameters={
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
        try:
            task_run_id = r.tasks[0].run_id
            output = w.jobs.get_run_output(task_run_id)
            if output.error:
                print(f"\nError:\n{output.error}")
            if output.error_trace:
                print(f"\nTrace:\n{output.error_trace[:3000]}")
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
            print(f"\nLogs (tail):\n{output.logs[-3000:]}")
    except Exception as e:
        print(f"  (could not fetch run output: {e})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--entity", default="dim_customer", help="Gold entity to build (default: dim_customer).")
    args = ap.parse_args()

    w = WorkspaceClient(host=WORKSPACE_HOST, auth_type="azure-cli")

    ws_path = upload_notebook(w, args.entity)
    run_id = submit_run(w, args.entity, ws_path)
    wait_for_run(w, run_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
