"""Package, deploy, and invoke the short-lived in-VPC DB bootstrap Lambda
(Task 8, ap-south-1).

What it does (idempotent end-to-end):
  1. Ensures the read-only DB secret ``mnre-chatbot-db-readonly`` exists in
     Secrets Manager (created here, by the deploy identity, because the
     tool-lambda role can READ ``mnre-chatbot-*`` but not CREATE secrets). The
     secret holds {host, port, dbname, username=mnre_readonly, password} with a
     generated password.
  2. Builds a deployment zip containing handler.py + the bundled schema.py +
     psycopg2 (manylinux x86_64 wheel for the Lambda runtime).
  3. Creates or updates the ``mnre-chatbot-db-bootstrap`` Lambda, attached to the
     2 private subnets + lambda-sg, reusing the existing tool-lambda role,
     timeout 120s / memory 512MB, with DB_* env vars.
  4. Invokes it synchronously and prints the returned summary.
  5. Writes ``db_readonly_secret_arn`` into infra/network_ids.json.

Run:  python infra/deploy_bootstrap.py
Requires the local AWS identity to have lambda/secretsmanager/iam:PassRole perms.
"""
from __future__ import annotations

import io
import json
import os
import secrets
import shutil
import string
import subprocess
import sys
import time
import zipfile

import boto3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import PROJECT, REGION, TAGS, load_ids, save_ids  # noqa: E402

HERE = os.path.dirname(__file__)
PKG_DIR = os.path.join(HERE, "bootstrap_db")
BUILD_DIR = os.path.join(HERE, "_bootstrap_build")
ZIP_PATH = os.path.join(HERE, "_bootstrap_db.zip")

FUNCTION_NAME = f"{PROJECT}-db-bootstrap"
READONLY_SECRET_NAME = f"{PROJECT}-db-readonly"
READONLY_USER = "mnre_readonly"
RUNTIME = "python3.12"
PSYCOPG_PKG = "psycopg2-binary==2.9.9"

lam = boto3.client("lambda", region_name=REGION)
sm = boto3.client("secretsmanager", region_name=REGION)


def _gen_password(n: int = 28) -> str:
    # URL/SQL-safe password: letters + digits only (avoids quoting headaches).
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def ensure_readonly_secret(ids: dict) -> str:
    """Create the read-only secret if missing; return its ARN.

    If it already exists we keep the existing password so re-runs stay
    idempotent (the Lambda will (re)apply that same password to the role).
    """
    payload = {
        "host": ids["db_endpoint"],
        "port": ids.get("db_port", 5432),
        "dbname": ids["db_name"],
        "username": READONLY_USER,
        "password": _gen_password(),
    }
    try:
        existing = sm.describe_secret(SecretId=READONLY_SECRET_NAME)
        print(f"[skip] readonly secret exists: {existing['ARN']}")
        return existing["ARN"]
    except sm.exceptions.ResourceNotFoundException:
        resp = sm.create_secret(
            Name=READONLY_SECRET_NAME,
            Description="MNRE chatbot read-only DB user (mnre_readonly) credentials",
            SecretString=json.dumps(payload),
            Tags=TAGS,
        )
        print(f"[create] readonly secret: {resp['ARN']}")
        return resp["ARN"]


def build_zip() -> bytes:
    """Build the deployment zip: handler + schema + psycopg2 (manylinux wheel)."""
    if os.path.isdir(BUILD_DIR):
        shutil.rmtree(BUILD_DIR)
    os.makedirs(BUILD_DIR)

    # Vendor psycopg2 built for the Lambda Linux runtime (manylinux x86_64,
    # cp312) regardless of the local machine arch/OS.
    print(f"[build] uv pip install {PSYCOPG_PKG} (manylinux2014_x86_64, cp312)")
    subprocess.check_call([
        "uv", "pip", "install",
        "--python-platform", "x86_64-manylinux2014",
        "--python-version", "3.12",
        "--only-binary", ":all:",
        "--target", BUILD_DIR,
        PSYCOPG_PKG,
    ])

    # Add our source files.
    for fname in ("handler.py", "schema.py"):
        shutil.copy(os.path.join(PKG_DIR, fname), os.path.join(BUILD_DIR, fname))

    # Zip the build dir.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(BUILD_DIR):
            for f in files:
                full = os.path.join(root, f)
                arc = os.path.relpath(full, BUILD_DIR)
                zf.write(full, arc)
    data = buf.getvalue()
    with open(ZIP_PATH, "wb") as fh:
        fh.write(data)
    print(f"[build] zip ready: {len(data)} bytes -> {ZIP_PATH}")
    return data


def deploy(ids: dict, zip_bytes: bytes, readonly_arn: str) -> None:
    env = {
        "Variables": {
            "DB_ENDPOINT": ids["db_endpoint"],
            "DB_NAME": ids["db_name"],
            "DB_PORT": str(ids.get("db_port", 5432)),
            "MASTER_SECRET_ARN": ids["db_master_secret_arn"],
            "READONLY_SECRET_NAME": READONLY_SECRET_NAME,
            "READONLY_USER": READONLY_USER,
        }
    }
    vpc_config = {
        "SubnetIds": ids["private_subnet_ids"],
        "SecurityGroupIds": [ids["lambda_sg"]],
    }
    role_arn = ids["tool_lambda_role_arn"]

    try:
        lam.get_function(FunctionName=FUNCTION_NAME)
        print(f"[update] {FUNCTION_NAME}")
        lam.update_function_code(FunctionName=FUNCTION_NAME, ZipFile=zip_bytes)
        _wait_updated()
        lam.update_function_configuration(
            FunctionName=FUNCTION_NAME,
            Runtime=RUNTIME,
            Handler="handler.handler",
            Role=role_arn,
            Timeout=120,
            MemorySize=512,
            Environment=env,
            VpcConfig=vpc_config,
        )
        _wait_updated()
    except lam.exceptions.ResourceNotFoundException:
        print(f"[create] {FUNCTION_NAME}")
        # IAM role propagation can lag right after creation; retry briefly.
        for attempt in range(6):
            try:
                lam.create_function(
                    FunctionName=FUNCTION_NAME,
                    Runtime=RUNTIME,
                    Role=role_arn,
                    Handler="handler.handler",
                    Code={"ZipFile": zip_bytes},
                    Timeout=120,
                    MemorySize=512,
                    Environment=env,
                    VpcConfig=vpc_config,
                    Tags=dict((t["Key"], t["Value"]) for t in TAGS),
                )
                break
            except lam.exceptions.InvalidParameterValueException as e:
                if "cannot be assumed" in str(e) and attempt < 5:
                    print(f"  role not ready, retrying ({attempt + 1})...")
                    time.sleep(5)
                    continue
                raise
        _wait_active()


def _wait_active() -> None:
    lam.get_waiter("function_active_v2").wait(FunctionName=FUNCTION_NAME)


def _wait_updated() -> None:
    lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION_NAME)


def invoke() -> dict:
    print(f"[invoke] {FUNCTION_NAME} (sync)")
    resp = lam.invoke(
        FunctionName=FUNCTION_NAME,
        InvocationType="RequestResponse",
        Payload=b"{}",
    )
    payload = resp["Payload"].read().decode()
    if resp.get("FunctionError"):
        print(f"[error] FunctionError={resp['FunctionError']}")
        print(payload)
        raise SystemExit(1)
    return json.loads(payload)


def main() -> None:
    ids = load_ids()
    print(f"Region {REGION}\n")
    readonly_arn = ensure_readonly_secret(ids)
    zip_bytes = build_zip()
    deploy(ids, zip_bytes, readonly_arn)
    summary = invoke()

    ids["db_readonly_secret_arn"] = readonly_arn
    save_ids(ids)

    print("\n=== Bootstrap summary ===")
    print(json.dumps(summary, indent=2))
    print("\ndb_readonly_secret_arn saved to network_ids.json")


if __name__ == "__main__":
    main()
