"""PipelineIQ control-plane HTTP endpoints.

Five endpoints consumed by ADF (or any caller with the function key) to
read/write the pipeline.* tables in PostgreSQL.

Routes (all under /api/, function-key auth):
  GET   /watermarks/{entity}                  → current watermark
  POST  /watermarks/{entity}/commit           → upsert watermark
  POST  /files/register                       → record landed file
  POST  /runs/start                           → open exec-log row (status=running)
  POST  /runs/{run_id}/end                    → close exec-log row (status=success|failed)

Postgres connection string comes from env var POSTGRES_URL (KV reference
to secret 'postgres-connection-string' in pipelineiq-kv-dev).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import azure.functions as func
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger("functions.api")

# ── Connection pool ──────────────────────────────────────────────────────────
# Lazy-initialised on first request. Reused across warm invocations.
# min_size=1 keeps one warm connection; max_size=4 caps per-instance fan-out
# since Functions scales horizontally — concurrency per instance is low.
_POOL: ConnectionPool | None = None


def _pool() -> ConnectionPool:
    global _POOL
    if _POOL is None:
        url = os.environ["POSTGRES_URL"]
        _POOL = ConnectionPool(
            conninfo=url,
            min_size=1,
            max_size=4,
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return _POOL


# ── Helpers ──────────────────────────────────────────────────────────────────

def _json_response(body: Any, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        body=json.dumps(body, default=str),
        status_code=status,
        mimetype="application/json",
    )


def _bad_request(msg: str) -> func.HttpResponse:
    return _json_response({"error": msg}, 400)


def _server_error(msg: str) -> func.HttpResponse:
    return _json_response({"error": msg}, 500)


def _parse_ts(value: Any) -> datetime:
    """Parse an ISO-8601 timestamp string; assume UTC if naive."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ── App ──────────────────────────────────────────────────────────────────────

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)


# ── Legacy V1 generator function, hoisted into V2 ─────────────────────────────
# Schedule "0 0 0 31 2 *" = Feb 31 = never fires (DECISIONS #69).
# Logic App `pipelineiq-scheduler-dev` admin-invokes this at 00:30 UTC daily
# via POST /admin/functions/generator — the function name must stay `generator`
# for the Logic App's URL + key wiring to keep working.
@app.function_name(name="generator")
@app.timer_trigger(
    schedule="0 0 0 31 2 *",
    arg_name="mytimer",
    run_on_startup=False,
    use_monitor=False,
)
def generator_timer(mytimer: func.TimerRequest) -> None:
    from generator.main import main as _generator_main
    _generator_main(mytimer)


@app.route(route="entities", methods=["GET"])
def get_entities(req: func.HttpRequest) -> func.HttpResponse:
    """Return the active entity_registry rows that drive the ADF master pipeline.

    Ordered by (priority, entity_name) so the caller's ForEach processes
    high-priority entities first. `partition_date_column` is non-null only
    for entities ADF extracts by business date (orders, inventory_snapshot);
    null means full-table dump.
    """
    try:
        with _pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT entity_name, source_schema, source_table,
                       watermark_column, load_type, partition_date_column,
                       priority
                FROM pipeline.entity_registry
                WHERE active = TRUE
                ORDER BY priority, entity_name
                """
            )
            rows = cur.fetchall()
        return _json_response({"entities": rows})
    except Exception as e:
        log.exception("get_entities failed: %s", e)
        return _server_error(str(e))


@app.route(route="watermarks/{entity}", methods=["GET"])
def get_watermark(req: func.HttpRequest) -> func.HttpResponse:
    entity = req.route_params["entity"]
    environment = req.params.get("environment", "dev")
    try:
        with _pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT entity_name, environment,
                       last_successful_load, next_window_start, updated_at
                FROM pipeline.watermarks
                WHERE entity_name = %s AND environment = %s
                """,
                (entity, environment),
            )
            row = cur.fetchone()
        if row is None:
            return _json_response({"error": "watermark not found",
                                   "entity": entity,
                                   "environment": environment}, 404)
        return _json_response(row)
    except Exception as e:
        log.exception("get_watermark failed: %s", e)
        return _server_error(str(e))


@app.route(route="watermarks/{entity}/commit", methods=["POST"])
def commit_watermark(req: func.HttpRequest) -> func.HttpResponse:
    entity = req.route_params["entity"]
    try:
        body = req.get_json()
    except ValueError:
        return _bad_request("body must be valid JSON")

    try:
        last_successful_load = _parse_ts(body["last_successful_load"])
        next_window_start = _parse_ts(body["next_window_start"])
    except (KeyError, ValueError) as e:
        return _bad_request(
            f"required keys: last_successful_load, next_window_start (ISO-8601). {e}"
        )
    environment = body.get("environment", "dev")

    try:
        with _pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pipeline.watermarks
                    (entity_name, environment, last_successful_load,
                     next_window_start, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (entity_name, environment) DO UPDATE
                SET last_successful_load = EXCLUDED.last_successful_load,
                    next_window_start    = EXCLUDED.next_window_start,
                    updated_at           = NOW()
                RETURNING entity_name, environment,
                          last_successful_load, next_window_start, updated_at
                """,
                (entity, environment, last_successful_load, next_window_start),
            )
            row = cur.fetchone()
        return _json_response(row)
    except Exception as e:
        log.exception("commit_watermark failed: %s", e)
        return _server_error(str(e))


@app.route(route="files/register", methods=["POST"])
def register_file(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
    except ValueError:
        return _bad_request("body must be valid JSON")

    required = ("file_path", "source_entity", "landed_at", "row_count", "pipeline_run_id")
    missing = [k for k in required if k not in body]
    if missing:
        return _bad_request(f"missing keys: {missing}")

    try:
        landed_at = _parse_ts(body["landed_at"])
        row_count = int(body["row_count"])
    except (ValueError, TypeError) as e:
        return _bad_request(f"invalid types: {e}")

    try:
        with _pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pipeline.file_registry
                    (file_path, source_entity, landed_at, row_count, pipeline_run_id)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING file_id, file_path, source_entity, landed_at,
                          row_count, pipeline_run_id, processed_flag
                """,
                (body["file_path"], body["source_entity"], landed_at,
                 row_count, body["pipeline_run_id"]),
            )
            row = cur.fetchone()
        return _json_response(row, 201)
    except Exception as e:
        log.exception("register_file failed: %s", e)
        return _server_error(str(e))


@app.route(route="runs/start", methods=["POST"])
def log_run_start(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
    except ValueError:
        return _bad_request("body must be valid JSON")

    required = ("run_id", "pipeline_name")
    missing = [k for k in required if k not in body]
    if missing:
        return _bad_request(f"missing keys: {missing}")

    start_time = _parse_ts(body["start_time"]) if "start_time" in body else datetime.now(timezone.utc)
    entity_name = body.get("entity_name")

    try:
        with _pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pipeline.pipeline_exec_log
                    (run_id, pipeline_name, entity_name, start_time, status)
                VALUES (%s, %s, %s, %s, 'running')
                RETURNING log_id, run_id, pipeline_name, entity_name,
                          start_time, status
                """,
                (body["run_id"], body["pipeline_name"], entity_name, start_time),
            )
            row = cur.fetchone()
        return _json_response(row, 201)
    except Exception as e:
        log.exception("log_run_start failed: %s", e)
        return _server_error(str(e))


@app.route(route="runs/{run_id}/end", methods=["POST"])
def log_run_end(req: func.HttpRequest) -> func.HttpResponse:
    run_id = req.route_params["run_id"]
    try:
        body = req.get_json()
    except ValueError:
        return _bad_request("body must be valid JSON")

    status = body.get("status")
    if status not in ("success", "failed"):
        return _bad_request("status must be 'success' or 'failed'")

    end_time = _parse_ts(body["end_time"]) if "end_time" in body else datetime.now(timezone.utc)

    try:
        with _pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE pipeline.pipeline_exec_log
                SET end_time      = %s,
                    status        = %s,
                    rows_read     = %s,
                    rows_written  = %s,
                    rows_rejected = %s,
                    error_message = %s
                WHERE run_id = %s AND end_time IS NULL
                RETURNING log_id, run_id, pipeline_name, entity_name,
                          start_time, end_time, status,
                          rows_read, rows_written, rows_rejected, error_message
                """,
                (
                    end_time,
                    status,
                    body.get("rows_read"),
                    body.get("rows_written"),
                    body.get("rows_rejected"),
                    body.get("error_message"),
                    run_id,
                ),
            )
            row = cur.fetchone()
        if row is None:
            return _json_response(
                {"error": "no open run found for run_id", "run_id": run_id}, 404
            )
        return _json_response(row)
    except Exception as e:
        log.exception("log_run_end failed: %s", e)
        return _server_error(str(e))
