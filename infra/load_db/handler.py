"""In-VPC loader Lambda for the MNRE chatbot Aurora (Task 9, ap-south-1).

Runs IN the default VPC's private subnets (reusing tool-lambda role + lambda_sg
+ private subnets) so it can reach the private Aurora over 5432 and read its
master secret via the Secrets Manager interface VPC endpoint. It reaches S3 via
the S3 GATEWAY VPC endpoint added to the private route table (no NAT).

Given ``{"table": "<t>"}`` it:
  1. streams ``s3://<LOAD_BUCKET>/<LOAD_PREFIX>/<t>.csv`` from S3,
  2. connects to Aurora as MASTER (write privileges) using the master secret,
  3. ``TRUNCATE``s the table (idempotent re-load),
  4. bulk-loads via ``COPY <t> (<curated cols>) FROM STDIN WITH
     (FORMAT csv, HEADER true, NULL '')`` (psycopg2 ``copy_expert``),
  5. returns ``{table, rows_loaded}`` where ``rows_loaded`` is a post-load
     ``SELECT count(*)``.

Credential VALUES are never logged (Req 12.4).
"""
from __future__ import annotations

import json
import os

import boto3
import psycopg2
from psycopg2 import sql

import schema  # bundled copy of src/common/schema.py

REGION = "ap-south-1"


def _get_secret(client, secret_id: str) -> dict:
    resp = client.get_secret_value(SecretId=secret_id)
    return json.loads(resp["SecretString"])


def _execute(cur, stmt):
    """Single choke point for statement execution.

    Every ``stmt`` passed here is a ``psycopg2.sql.Composed`` built exclusively
    from ``sql.SQL`` literals and ``sql.Identifier`` (safe identifier quoting)
    for a table name already validated against ``schema.TABLES``.
    """
    cur.execute(stmt)


def _copy_sql(table: str) -> sql.Composed:
    """COPY statement loading exactly the curated columns.

    The table name is validated against ``schema.TABLES`` before this is called
    and every identifier is composed with ``psycopg2.sql.Identifier`` (safe
    quoting) — no string interpolation into the SQL text.
    """
    cols = sql.SQL(", ").join(sql.Identifier(c) for c in schema.CURATED_COLUMNS)
    return sql.SQL(
        "COPY {} ({}) FROM STDIN WITH (FORMAT csv, HEADER true, NULL '')"
    ).format(sql.Identifier(table), cols)


def handler(event, context):  # noqa: ARG001 - Lambda signature
    table = event.get("table")
    if table not in schema.TABLES:
        return {"ok": False, "error": f"unsupported table '{table}'"}

    db_endpoint = os.environ["DB_ENDPOINT"]
    db_name = os.environ["DB_NAME"]
    db_port = int(os.environ.get("DB_PORT", "5432"))
    master_secret_arn = os.environ["MASTER_SECRET_ARN"]
    bucket = os.environ["LOAD_BUCKET"]
    prefix = os.environ.get("LOAD_PREFIX", "chatbot-load")
    key = f"{prefix}/{table}.csv"

    s3 = boto3.client("s3", region_name=REGION)
    sm = boto3.client("secretsmanager", region_name=REGION)
    master = _get_secret(sm, master_secret_arn)

    print(
        f"loading table={table} from s3://{bucket}/{key} "
        f"into host={db_endpoint} db={db_name} as user={master['username']}"
    )

    obj = s3.get_object(Bucket=bucket, Key=key)
    body = obj["Body"]  # botocore StreamingBody — file-like, supports .read()

    conn = psycopg2.connect(
        host=db_endpoint,
        port=db_port,
        dbname=db_name,
        user=master["username"],
        password=master["password"],
        connect_timeout=10,
        sslmode="require",
    )
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            # `table` is validated against schema.TABLES above and composed with
            # psycopg2.sql.Identifier (safe identifier quoting) — not raw input.
            _execute(cur, sql.SQL("TRUNCATE TABLE {};").format(sql.Identifier(table)))
            cur.copy_expert(_copy_sql(table), body)
        conn.commit()
        with conn.cursor() as cur:
            _execute(cur, sql.SQL("SELECT count(*) FROM {};").format(sql.Identifier(table)))
            rows_loaded = cur.fetchone()[0]
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    summary = {"ok": True, "table": table, "rows_loaded": int(rows_loaded)}
    print(f"summary: {json.dumps(summary)}")
    return summary
