"""Read-only Tool_Lambda handler for the MNRE chatbot (Task 10.1, ap-south-1).

Runs IN the default VPC's private subnets so it can reach the private Aurora
cluster directly over 5432 (TLS) and read the READ-ONLY DB credential via the
Secrets Manager interface VPC endpoint — no NAT (see design Network section).

Contract (Parameterized_Query_API). The Gateway pins ``table`` per tool and
forwards the agent-supplied query shape::

    {
      "table": "subsidy",                 # fixed per Gateway tool
      "filters": [{"column","op","value"}],
      "group_by": ["..."],
      "aggregations": [{"fn","column","alias"}],
      "limit": 100
    }

Flow (pure-logic modules are bundled alongside this handler):
  1. ``validate_request`` — reject unknown table/column/op/fn or raw SQL with
     ``{"executed": false, "error": ...}`` and run NOTHING (Req 5.6, 5.10, 11.7).
  2. ``build_query`` — SELECT-only, whitelist-only identifiers, every literal
     bound as a psycopg ``%s`` parameter (Req 5.1, 5.4, 5.5).
  3. connect to Aurora as the READ-ONLY user (secret), execute the parameterized
     SELECT, ``shape_response`` the rows (Req 5.2, 5.7).
Credential VALUES are never logged — all log lines pass through ``redact``
(Req 11.6, 12.4).
"""
from __future__ import annotations

import json
import os

import boto3
import psycopg2

from common.schema import TABLES
from tool.query_builder import build_query, clamp_limit
from tool.response import redact, shape_response
from tool.validate import validate_request

REGION = "ap-south-1"

# Cached across warm invocations.
_secret_cache: dict | None = None


def _log(message: str, secret: dict | None = None) -> None:
    """Print a log line with any credential values redacted (Req 11.6, 12.4)."""
    print(redact(message, secret) if secret else message)


def _execute(cur, stmt: str, params: list) -> None:
    """Single choke point for query execution (Req 5.1, 5.4, 5.5).

    ``stmt`` always comes from ``tool.query_builder.build_query`` — a SELECT
    built only from whitelisted identifiers (``common.schema``) after
    ``validate_request`` — and every literal value is bound via ``params``
    (%s placeholders). Nothing user-controlled is interpolated into the SQL.
    """
    cur.execute(stmt, params)


def _get_readonly_secret() -> dict:
    """Fetch + cache the READ-ONLY DB credential from Secrets Manager."""
    global _secret_cache
    if _secret_cache is None:
        secret_arn = os.environ["READONLY_SECRET_ARN"]
        sm = boto3.client("secretsmanager", region_name=REGION)
        resp = sm.get_secret_value(SecretId=secret_arn)
        _secret_cache = json.loads(resp["SecretString"])
    return _secret_cache


def _build_request(event: dict) -> dict:
    """Extract the Parameterized_Query_API request from the Lambda event.

    AgentCore Gateway forwards the query shape as the event body. We pass the
    recognized contract fields straight through to ``validate_request`` (which
    still rejects any stray/raw-SQL key present on the event).
    """
    return event if isinstance(event, dict) else {"table": None}


def handler(event, context):  # noqa: ARG001 - Lambda signature
    req = _build_request(event)

    # 1) Validate against the whitelist; on any violation, run NOTHING.
    error = validate_request(req)
    if error is not None:
        _log(f"rejected request: {error['error']}")
        return error

    table = req["table"]
    effective_limit = clamp_limit(req.get("limit"))

    # 2) Build the SELECT-only, fully parameterized statement.
    sql, params = build_query(req)

    # 3) Connect as the READ-ONLY user and execute.
    secret = _get_readonly_secret()
    db_endpoint = os.environ["DB_ENDPOINT"]
    db_name = os.environ["DB_NAME"]
    db_port = int(os.environ.get("DB_PORT", "5432"))

    _log(
        f"executing on table={table} host={db_endpoint} db={db_name} "
        f"as user={secret.get('username')} sql={sql}",
        secret,
    )

    conn = None
    try:
        conn = psycopg2.connect(
            host=db_endpoint,
            port=db_port,
            dbname=db_name,
            user=secret["username"],
            password=secret["password"],
            connect_timeout=10,
            sslmode="require",
        )
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor() as cur:
            # Safe by construction: `sql` is produced by tool.query_builder from
            # whitelist-only identifiers (common.schema) after validate_request;
            # every literal value is bound via `params` (%s placeholders). The
            # session is read-only and the DB user is SELECT-only.
            _execute(cur, sql, params)
            columns = [d[0] for d in cur.description]
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 - return structured error, never leak creds
        _log(f"db error: {type(exc).__name__}: {exc}", secret)
        return {
            "executed": False,
            "error": f"database error: {type(exc).__name__}",
        }
    finally:
        if conn is not None:
            conn.close()

    truncated = len(rows) >= effective_limit
    response = shape_response(rows, columns, table, truncated=truncated)
    _log(f"ok table={table} row_count={response['row_count']} truncated={truncated}")
    return response


if __name__ == "__main__":
    # Local dry-run of the pure path (no DB): prints the built SQL for a sample.
    sample = {
        "table": "subsidy",
        "filters": [{"column": "duplicate_bank_account_number", "op": "eq", "value": True}],
        "aggregations": [{"fn": "count", "column": "application_id", "alias": "n"}],
    }
    print(validate_request(sample))
    print(build_query(sample))
