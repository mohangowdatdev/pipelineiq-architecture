"""Multi-task Databricks Job for medallion catch-up.

One Job per layer (bronze/silver/gold). All entities for a layer run as
parallel tasks on a single shared cluster — pays for ONE cold start
across all entities. Gold mode wires task dependencies (dims → facts → rollup).

Usage:
    .venv/bin/python scripts/catchup_medallion.py --layer bronze
    .venv/bin/python scripts/catchup_medallion.py --layer silver
    .venv/bin/python scripts/catchup_medallion.py --layer gold

Auth: AAD via DefaultAzureCredential (your `az login` session).

Cost model (Central India, Jobs Compute, DS3_v2 with 4 workers):
- Cold start: ~3 min × 1 cluster = ~3 cluster-min
- Work: ~2 min per task in parallel (4-way) = ~5 min wall for 10 tasks
- Total: ~8-10 min wall per layer, ~Rs.15 per layer
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
REPO_ROOT = Path(__file__).resolve().parent.parent

# Orchestrator: single notebook ADF invokes to chain bronze→silver→gold.
ORCHESTRATOR_LOCAL = REPO_ROOT / "notebooks/orchestrate_medallion.py"
ORCHESTRATOR_WS = "/Shared/pipelineiq/orchestrate_medallion"

# Bronze: one notebook handles every entity via widget; single upload, N tasks.
BRONZE_NOTEBOOK_LOCAL = REPO_ROOT / "notebooks/bronze/ingest_to_bronze.py"
BRONZE_NOTEBOOK_WS = "/Shared/pipelineiq/bronze/ingest_to_bronze"
BRONZE_ENTITIES = [
    "orders",
    "order_lines",
    "order_status_log",
    "customers",
    "customer_addresses",
    "products",
    "product_pricing",
    "inventory_snapshot",
    "sales_reps",
    "territory_assignments",
]

# Silver: one notebook per entity.
SILVER_DIR_LOCAL = REPO_ROOT / "notebooks/silver"
SILVER_DIR_WS = "/Shared/pipelineiq/silver"
SILVER_ENTITIES = [
    "orders",
    "order_lines",
    "order_status_log",
    "customers",
    "customer_addresses",
    "products",
    "product_pricing",
    "inventory_snapshot",
    "sales_reps",
    "territory_assignments",
]

# Gold: one notebook per dim/fact, except static_dims which builds 5.
GOLD_DIR_LOCAL = REPO_ROOT / "notebooks/gold"
GOLD_DIR_WS = "/Shared/pipelineiq/gold"
GOLD_TASKS = [
    # (entity, depends_on)
    ("dim_customer", []),
    ("dim_product", []),
    ("dim_sales_rep", []),
    ("dim_territory", []),
    ("static_dims", []),
    ("fact_order_line", ["dim_customer", "dim_product", "dim_sales_rep", "dim_territory", "static_dims"]),
    ("fact_inventory_daily", ["dim_product", "static_dims"]),
    ("fact_daily_channel_revenue", ["fact_order_line", "static_dims"]),
]

SHARED_CLUSTER_KEY = "catchup_cluster"


def upload(w: WorkspaceClient, local: Path, ws_path: str) -> None:
    parent = ws_path.rsplit("/", 1)[0]
    w.workspace.mkdirs(parent)
    with local.open("rb") as f:
        content = f.read()
    w.workspace.upload(
        path=ws_path,
        content=content,
        format=ImportFormat.SOURCE,
        language=Language.PYTHON,
        overwrite=True,
    )
    print(f"  uploaded {local.name} ({len(content):,} bytes) → {ws_path}")


def upload_all_for_layer(w: WorkspaceClient, layer: str) -> dict[str, str]:
    """Returns entity → workspace path mapping."""
    mapping: dict[str, str] = {}
    if layer == "bronze":
        upload(w, BRONZE_NOTEBOOK_LOCAL, BRONZE_NOTEBOOK_WS)
        for e in BRONZE_ENTITIES:
            mapping[e] = BRONZE_NOTEBOOK_WS
    elif layer == "silver":
        for e in SILVER_ENTITIES:
            local = SILVER_DIR_LOCAL / f"build_silver_{e}.py"
            ws = f"{SILVER_DIR_WS}/build_silver_{e}"
            upload(w, local, ws)
            mapping[e] = ws
    elif layer == "gold":
        for entity, _ in GOLD_TASKS:
            local = GOLD_DIR_LOCAL / f"build_gold_{entity}.py"
            ws = f"{GOLD_DIR_WS}/build_gold_{entity}"
            upload(w, local, ws)
            mapping[entity] = ws
    return mapping


def build_tasks(layer: str, mapping: dict[str, str], run_id: str) -> list[jobs.Task]:
    tasks: list[jobs.Task] = []
    if layer == "bronze":
        for e in BRONZE_ENTITIES:
            tasks.append(
                jobs.Task(
                    task_key=f"bronze_{e}",
                    notebook_task=jobs.NotebookTask(
                        notebook_path=mapping[e],
                        base_parameters={
                            "entity_name": e,
                            "pipeline_run_id": run_id,
                        },
                    ),
                    job_cluster_key=SHARED_CLUSTER_KEY,
                )
            )
    elif layer == "silver":
        for e in SILVER_ENTITIES:
            tasks.append(
                jobs.Task(
                    task_key=f"silver_{e}",
                    notebook_task=jobs.NotebookTask(
                        notebook_path=mapping[e],
                        base_parameters={
                            "pipeline_run_id": run_id,
                        },
                    ),
                    job_cluster_key=SHARED_CLUSTER_KEY,
                )
            )
    elif layer == "gold":
        for entity, deps in GOLD_TASKS:
            tasks.append(
                jobs.Task(
                    task_key=f"gold_{entity}",
                    notebook_task=jobs.NotebookTask(
                        notebook_path=mapping[entity],
                        base_parameters={
                            "pipeline_run_id": run_id,
                        },
                    ),
                    depends_on=[jobs.TaskDependency(task_key=f"gold_{d}") for d in deps] or None,
                    job_cluster_key=SHARED_CLUSTER_KEY,
                )
            )
    return tasks


def submit_layer(w: WorkspaceClient, layer: str) -> tuple[int, int]:
    """Returns (job_id, run_id). Caller is responsible for deleting the job after the run."""
    print(f"\n=== Uploading notebooks for {layer} ===")
    mapping = upload_all_for_layer(w, layer)

    run_id = str(uuid.uuid4())
    print(f"\n=== Creating {layer} multi-task Job (pipeline_run_id={run_id}) ===")

    cluster = compute.ClusterSpec(
        spark_version="14.3.x-scala2.12",
        node_type_id="Standard_DS3_v2",
        num_workers=4,
        data_security_mode=DataSecurityMode.SINGLE_USER,
    )

    job = w.jobs.create(
        name=f"catchup-{layer}-{run_id[:8]}",
        tasks=build_tasks(layer, mapping, run_id),
        job_clusters=[
            jobs.JobCluster(job_cluster_key=SHARED_CLUSTER_KEY, new_cluster=cluster),
        ],
        max_concurrent_runs=1,
    )
    print(f"  job_id = {job.job_id}")

    triggered = w.jobs.run_now(job_id=job.job_id)
    print(f"  run_id = {triggered.run_id}")
    return job.job_id, triggered.run_id


def wait_for_run(w: WorkspaceClient, run_id: int) -> bool:
    print(f"\n=== Waiting on run {run_id} ===")
    while True:
        r = w.jobs.get_run(run_id)
        state = r.state
        life_cycle = state.life_cycle_state.value if state and state.life_cycle_state else "?"
        result = state.result_state.value if state and state.result_state else None
        n_running = sum(1 for t in (r.tasks or []) if t.state and t.state.life_cycle_state and t.state.life_cycle_state.value == "RUNNING")
        n_done = sum(1 for t in (r.tasks or []) if t.state and t.state.result_state and t.state.result_state.value == "SUCCESS")
        n_failed = sum(1 for t in (r.tasks or []) if t.state and t.state.result_state and t.state.result_state.value != "SUCCESS")
        print(f"  {life_cycle}{f' / {result}' if result else ''}   tasks: running={n_running} done={n_done} failed={n_failed}")
        if life_cycle in ("TERMINATED", "INTERNAL_ERROR", "SKIPPED"):
            break
        time.sleep(20)

    ok = r.state.result_state and r.state.result_state.value == "SUCCESS"
    if not ok:
        print(f"\nFAILED: {r.state.state_message}")
        for t in (r.tasks or []):
            if t.state and t.state.result_state and t.state.result_state.value != "SUCCESS":
                print(f"  {t.task_key}: {t.state.result_state.value} — {t.state.state_message}")
                try:
                    out = w.jobs.get_run_output(t.run_id)
                    if out.error_trace:
                        print(f"    trace: {out.error_trace[:1500]}")
                except Exception:
                    pass
    else:
        print(f"\nSUCCESS — all tasks green")
    return ok


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--layer", choices=["bronze", "silver", "gold"])
    p.add_argument(
        "--upload-orchestrator",
        action="store_true",
        help="Upload notebooks/orchestrate_medallion.py to the workspace and exit "
        "(no job run). The ADF master pipeline invokes this notebook.",
    )
    p.add_argument("--keep-job", action="store_true", help="Don't delete the job after run (useful for debug)")
    args = p.parse_args()

    w = WorkspaceClient(host=WORKSPACE_HOST)

    if args.upload_orchestrator:
        print("=== Uploading medallion orchestrator ===")
        upload(w, ORCHESTRATOR_LOCAL, ORCHESTRATOR_WS)
        sys.exit(0)

    if not args.layer:
        p.error("one of --layer or --upload-orchestrator is required")

    job_id, run_id = submit_layer(w, args.layer)
    try:
        ok = wait_for_run(w, run_id)
    finally:
        if not args.keep_job:
            try:
                w.jobs.delete(job_id=job_id)
                print(f"\n(cleaned up: deleted job_id={job_id})")
            except Exception as e:
                print(f"\n(warning: couldn't delete job_id={job_id}: {e})")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
