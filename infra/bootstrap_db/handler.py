"""DB bootstrap Lambda for the MNRE chatbot Aurora (ap-south-1, Task 8).

Runs IN the default VPC's private subnets so it can reach the private Aurora
cluster over 5432, and reads its secrets via the Secrets Manager interface VPC
endpoint (no NAT). It is SHORT-LIVED: invoked once (or re-invoked; it is
idempotent) to:

  1. CREATE the 4 curated tables + their indexes (DDL imported from the bundled
     copy of ``schema.py`` — the single source of truth, Req 3).
  2. CREATE a read-only DB role ``mnre_readonly`` with SELECT-only grants
     (Req 11.5): CONNECT on the db, USAGE on schema public, SELECT on all
     existing tables, and ALTER DEFAULT PRIVILEGES so future tables are also
     SELECT-able. Idempotent (DO block for the role; IF NOT EXISTS for tables).
  3. Return a JSON summary (tables/indexes created, readonly user, the list of
     tables seen in information_schema).

Credentials:
  - MASTER secret (``MASTER_SECRET_ARN``): the RDS-managed master creds
    (user ``mnre_admin``) used to connect and run the DDL/grants.
  - READONLY secret (``READONLY_SECRET_NAME``): pre-created by the deploy
    script; holds the generated password for ``mnre_readonly``. The handler
    reads it and applies that password to the role so the Tool_Lambda can later
    use the same secret to connect.

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
    """Fetch a Secrets Manager secret and parse its JSON SecretString."""
    resp = client.get_secret_value(SecretId=secret_id)
    return json.loads(resp["SecretString"])


def _execute(cur, stmt, params=None):
    """Single choke point for statement execution.

    Every ``stmt`` passed here is either a string literal or a
    ``psycopg2.sql.Composed`` built exclusively from ``sql.Identifier`` /
    ``sql.SQL`` literals (safe identifier quoting; Postgres cannot parameterize
    identifiers in DDL). Every literal value travels in ``params`` as a bound
    ``%s`` parameter — nothing user-controlled is ever interpolated into SQL.
    """
    if params is None:
        cur.execute(stmt)
    else:
        cur.execute(stmt, params)


def _create_tables(cur) -> tuple[list[str], list[str]]:
    """Run CREATE TABLE + CREATE INDEX (idempotent) for all curated tables."""
    tables_created: list[str] = []
    indexes_created: list[str] = []
    for table in schema.TABLES:
        cur.execute(schema.ddl_for_table(table))
        tables_created.append(table)
        for stmt in schema.index_ddl_for_table(table):
            cur.execute(stmt)
            indexes_created.append(stmt.split()[5])  # idx name
    return tables_created, indexes_created


def _create_readonly_user(cur, dbname: str, username: str, password: str) -> None:
    """Create/refresh the read-only role with SELECT-only grants (idempotent).

    The role name is a trusted constant (interpolated as a quoted identifier);
    the password is bound as a psycopg2 parameter so it is safely quoted as a
    string literal and never appears raw in the SQL text.
    """
    # Create the role if it does not exist, else (re)set its password so it
    # matches the secret. Existence is checked in Python to avoid a DO block.
    # All identifiers are composed with psycopg2.sql.Identifier (safe quoting);
    # the password is bound as a parameter so it never appears in the SQL text.
    # DDL statements below cannot use %s placeholders for role/database names
    # (Postgres does not allow parameterized identifiers). Identifiers come from
    # trusted deploy-time constants (env vars set by our own IaC, never request
    # input) and are composed with psycopg2.sql.Identifier for safe quoting; the
    # password is bound as a %s parameter via _execute().
    user_ident = sql.Identifier(username)
    _execute(cur, "SELECT 1 FROM pg_roles WHERE rolname = %s;", (username,))
    exists = cur.fetchone() is not None
    if exists:
        _execute(
            cur,
            sql.SQL("ALTER ROLE {} LOGIN PASSWORD %s;").format(user_ident),
            (password,),
        )
    else:
        _execute(
            cur,
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD %s;").format(user_ident),
            (password,),
        )
    # SELECT-only grants.
    _execute(
        cur,
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {};").format(
            sql.Identifier(dbname), user_ident
        ),
    )
    _execute(cur, sql.SQL("GRANT USAGE ON SCHEMA public TO {};").format(user_ident))
    _execute(
        cur,
        sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {};").format(user_ident),
    )
    _execute(
        cur,
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {};"
        ).format(user_ident),
    )
    # Defensively ensure no write privileges linger from a prior run.
    _execute(
        cur,
        sql.SQL(
            "REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public "
            "FROM {};"
        ).format(user_ident),
    )


def _verify_readonly(db_endpoint, db_port, db_name, readonly) -> dict:
    """Connect AS the read-only user and prove it can SELECT but not INSERT."""
    conn = psycopg2.connect(
        host=db_endpoint,
        port=db_port,
        dbname=db_name,
        user=readonly["username"],
        password=readonly["password"],
        connect_timeout=10,
        sslmode="require",
    )
    conn.autocommit = True
    result = {"can_select": False, "insert_blocked": False}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM subsidy;")
            cur.fetchone()
            result["can_select"] = True
            try:
                cur.execute(
                    "INSERT INTO subsidy (application_id) VALUES ('verify-probe');"
                )
                result["insert_blocked"] = False  # should not reach here
            except psycopg2.errors.InsufficientPrivilege:
                result["insert_blocked"] = True
    finally:
        conn.close()
    return result


def _column_types(cur, table: str) -> dict:
    """Return {column: data_type} for a table (verification of DDL types)."""
    cur.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position;",
        (table,),
    )
    return {r[0]: r[1] for r in cur.fetchall()}


def handler(event, context):  # noqa: ARG001 - Lambda signature
    db_endpoint = os.environ["DB_ENDPOINT"]
    db_name = os.environ["DB_NAME"]
    db_port = int(os.environ.get("DB_PORT", "5432"))
    master_secret_arn = os.environ["MASTER_SECRET_ARN"]
    readonly_secret_name = os.environ["READONLY_SECRET_NAME"]
    readonly_user = os.environ.get("READONLY_USER", "mnre_readonly")

    sm = boto3.client("secretsmanager", region_name=REGION)
    master = _get_secret(sm, master_secret_arn)
    readonly = _get_secret(sm, readonly_secret_name)

    print(
        f"connecting host={db_endpoint} db={db_name} port={db_port} "
        f"as user={master['username']}"  # value of password NOT logged
    )

    conn = psycopg2.connect(
        host=db_endpoint,
        port=db_port,
        dbname=db_name,
        user=master["username"],
        password=master["password"],
        connect_timeout=10,
        sslmode="require",
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            tables_created, indexes_created = _create_tables(cur)
            _create_readonly_user(
                cur, db_name, readonly_user, readonly["password"]
            )
            # Verify: list tables present in the public schema.
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' ORDER BY table_name;"
            )
            present = [r[0] for r in cur.fetchall()]
            # Verify: column types for one table (all four are identical).
            subsidy_types = _column_types(cur, "subsidy")
    finally:
        conn.close()

    # Verify the read-only user really is SELECT-only by connecting AS it.
    readonly_check = _verify_readonly(db_endpoint, db_port, db_name, readonly)

    summary = {
        "ok": True,
        "tables_created": tables_created,
        "index_count": len(indexes_created),
        "indexes": indexes_created,
        "readonly_user": readonly_user,
        "readonly_grants": "CONNECT, USAGE, SELECT (+ default privileges)",
        "readonly_check": readonly_check,
        "tables_present": present,
        "tables_present_count": len(present),
        "subsidy_column_types": subsidy_types,
    }
    print(f"summary: {json.dumps(summary)}")
    return summary
