"""Upload + smoke-test the source-sim inventory-snapshot notebook.

Usage:
    .venv/bin/python scripts/run_inventory_smoke.py --date 2026-05-25
    .venv/bin/python scripts/run_inventory_smoke.py --date 2026-05-25 --force
    .venv/bin/python scripts/run_inventory_smoke.py --date 2026-05-25 --force \
        --no-verify-orders

Auth: AAD via DefaultAzureCredential (your `az login` session).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.compute import DataSecurityMode
from databricks.sdk.service import jobs, compute
from databricks.sdk.service.workspace import ImportFormat, Language

WORKSPACE_HOST = "https://adb-7405617631498102.2.azuredatabricks.net"
LOCAL_NOTEBOOK = (
    Path(__file__).resolve().parent.parent
    / "notebooks/source_sim/write_inventory_snapshot.py"
)
WS_NOTEBOOK_PATH = "/Shared/pipelineiq/source_sim/write_inventory_snapshot"
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


def submit_run(w: WorkspaceClient, date_str: str, force: bool,
               verify_orders: bool) -> int:
    print(
        f"\nSubmitting one-time run: snapshot_date={date_str} "
        f"force={force} verify_order_landed={verify_orders}"
    )

    run = w.jobs.submit(
        run_name=f"inventory-smoke-{date_str}",
        tasks=[
            jobs.SubmitTask(
                task_key="inventory",
                notebook_task=jobs.NotebookTask(
                    notebook_path=WS_NOTEBOOK_PATH,
                    base_parameters={
                        "snapshot_date": date_str,
                        "force": "true" if force else "false",
                        "verify_order_landed": "true" if verify_orders else "false",
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
            out = w.jobs.get_run_output(task_run_id)
            if out.error_trace:
                print("\n--- error trace ---")
                print(out.error_trace)
            if out.logs:
                print("\n--- logs (tail) ---")
                print(out.logs[-4000:])
        except Exception as e:
            print(f"  (couldn't fetch run output: {e})")
        sys.exit(1)
    else:
        print(f"\nSUCCESS")
        try:
            task_run_id = r.tasks[0].run_id
            out = w.jobs.get_run_output(task_run_id)
            if out.notebook_output and out.notebook_output.result:
                print(f"  notebook exit value: {out.notebook_output.result}")
        except Exception:
            pass


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--date", required=True, help="YYYY-MM-DD or 'yesterday_utc'")
    p.add_argument("--force", action="store_true",
                   help="Wipe existing rows for the date first")
    p.add_argument("--no-verify-orders", action="store_true",
                   help="Skip the velora_oms.orders pre-check")
    args = p.parse_args()

    w = WorkspaceClient(host=WORKSPACE_HOST)
    upload_notebook(w)
    run_id = submit_run(w, args.date, args.force, not args.no_verify_orders)
    wait_for_run(w, run_id)


if __name__ == "__main__":
    main()
