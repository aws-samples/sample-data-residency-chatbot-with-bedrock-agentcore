"""Package, deploy, and integration-test the read-only Tool_Lambda (Task 10, ap-south-1).

End-to-end, idempotent:
  1. Build a deployment zip preserving the import packages the handler uses:
       tool/    (handler.py, validate.py, query_builder.py, response.py, __init__.py)
       common/  (schema.py, __init__.py)
     plus psycopg2 (manylinux2014_x86_64, cp312 wheel) at the zip root — exactly
     the packaging approach used by the loader Lambda (deploy_loader.py).
  2. Create/update the ``mnre-chatbot-tool`` Lambda in the 2 private subnets +
     lambda-sg, role = tool_lambda_role_arn, handler ``tool.handler.handler``,
     timeout 30s / memory 512MB; env DB_ENDPOINT/DB_NAME/DB_PORT/READONLY_SECRET_ARN.
  3. Run a few integration invocations against the LOADED (partial) data and an
     INVALID request; print the structured JSON responses.
  4. Save ``tool_lambda_arn`` to network_ids.json.

Run:  uv run python infra/deploy_tool.py
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import zipfile

import boto3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import PROJECT, REGION, TAGS_MAP, load_ids, save_ids  # noqa: E402

HERE = os.path.dirname(__file__)
SRC = os.path.join(HERE, "..", "src")
TOOL_SRC = os.path.join(SRC, "tool")
COMMON_SRC = os.path.join(SRC, "common")
BUILD_DIR = os.path.join(HERE, "_tool_build")
ZIP_PATH = os.path.join(HERE, "_tool.zip")

FUNCTION_NAME = f"{PROJECT}-tool"
RUNTIME = "python3.12"
HANDLER = "tool.handler.handler"
PSYCOPG_PKG = "psycopg2-binary==2.9.9"

# Tool source modules to bundle (self-contained zip per task requirement).
TOOL_MODULES = ("__init__.py", "handler.py", "validate.py", "query_builder.py", "response.py")
COMMON_MODULES = ("__init__.py", "schema.py")

lam = boto3.client("lambda", region_name=REGION)


def build_zip() -> bytes:
    """Build the zip: tool/ + common/ packages + psycopg2 (manylinux wheel)."""
    if os.path.isdir(BUILD_DIR):
        shutil.rmtree(BUILD_DIR)
    os.makedirs(BUILD_DIR)

    print(f"[build] pip install {PSYCOPG_PKG} (manylinux2014_x86_64, cp312)")
    subprocess.check_call([
        "uv", "pip", "install",
        "--python-platform", "x86_64-manylinux2014",
        "--python-version", "3.12",
        "--only-binary", ":all:",
        "--target", BUILD_DIR,
        PSYCOPG_PKG,
    ])

    # tool/ package (self-contained: handler + the three pure-logic modules).
    tool_dst = os.path.join(BUILD_DIR, "tool")
    os.makedirs(tool_dst, exist_ok=True)
    for m in TOOL_MODULES:
        shutil.copy(os.path.join(TOOL_SRC, m), os.path.join(tool_dst, m))

    # common/ package (schema.py — single source of truth, copied fresh).
    common_dst = os.path.join(BUILD_DIR, "common")
    os.makedirs(common_dst, exist_ok=True)
    for m in COMMON_MODULES:
        shutil.copy(os.path.join(COMMON_SRC, m), os.path.join(common_dst, m))

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


def deploy(ids: dict, zip_bytes: bytes) -> str:
    env = {
        "Variables": {
            "DB_ENDPOINT": ids["db_endpoint"],
            "DB_NAME": ids["db_name"],
            "DB_PORT": str(ids.get("db_port", 5432)),
            "READONLY_SECRET_ARN": ids["db_readonly_secret_arn"],
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
            Handler=HANDLER,
            Role=role_arn,
            Timeout=30,
            MemorySize=512,
            Environment=env,
            VpcConfig=vpc_config,
        )
        _wait_updated()
    except lam.exceptions.ResourceNotFoundException:
        print(f"[create] {FUNCTION_NAME}")
        import time
        for attempt in range(6):
            try:
                lam.create_function(
                    FunctionName=FUNCTION_NAME,
                    Runtime=RUNTIME,
                    Role=role_arn,
                    Handler=HANDLER,
                    Code={"ZipFile": zip_bytes},
                    Timeout=30,
                    MemorySize=512,
                    Environment=env,
                    VpcConfig=vpc_config,
                    Tags=TAGS_MAP,
                )
                break
            except lam.exceptions.InvalidParameterValueException as e:
                if "cannot be assumed" in str(e) and attempt < 5:
                    print(f"  role not ready, retrying ({attempt + 1})...")
                    time.sleep(5)
                    continue
                raise
        _wait_active()

    arn = lam.get_function(FunctionName=FUNCTION_NAME)["Configuration"]["FunctionArn"]
    print(f"[ok] {FUNCTION_NAME} -> {arn}")
    return arn


def _wait_active() -> None:
    lam.get_waiter("function_active_v2").wait(FunctionName=FUNCTION_NAME)


def _wait_updated() -> None:
    lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION_NAME)


def invoke(payload: dict) -> dict:
    resp = lam.invoke(
        FunctionName=FUNCTION_NAME,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode(),
    )
    body = resp["Payload"].read().decode()
    if resp.get("FunctionError"):
        print(f"[error] FunctionError={resp['FunctionError']}")
        print(body)
        raise SystemExit(1)
    return json.loads(body)


# --------------------------------------------------------------------------- #
# Integration requests (Part C) — assert the queries WORK against partial data.
# --------------------------------------------------------------------------- #
INTEGRATION_REQUESTS = [
    (
        "1. applications: count where installation_status='Installation Pending'",
        {
            "table": "applications",
            "filters": [
                {"column": "installation_status", "op": "eq",
                 "value": "Installation Pending"}
            ],
            "aggregations": [
                {"fn": "count", "column": "application_id", "alias": "n"}
            ],
        },
    ),
    (
        "2. subsidy: count where duplicate_bank_account_number=true",
        {
            "table": "subsidy",
            "filters": [
                {"column": "duplicate_bank_account_number", "op": "eq", "value": True}
            ],
            "aggregations": [
                {"fn": "count", "column": "application_id", "alias": "n"}
            ],
        },
    ),
    (
        "3. subsidy: sum disbursement_tranche_1_amount_inr by name_of_bank limit 5",
        {
            "table": "subsidy",
            "group_by": ["name_of_bank"],
            "aggregations": [
                {"fn": "sum", "column": "disbursement_tranche_1_amount_inr",
                 "alias": "disbursed"}
            ],
            "limit": 5,
        },
    ),
    (
        "4. INVALID: raw SQL / unknown column -> error, executed=false",
        {
            "table": "subsidy",
            "sql": "SELECT * FROM subsidy; DROP TABLE subsidy;",
            "filters": [{"column": "definitely_not_a_column", "op": "eq", "value": 1}],
        },
    ),
]


def run_integration() -> None:
    print("\n=== integration invocations (partial loaded data) ===")
    for label, payload in INTEGRATION_REQUESTS:
        print(f"\n--- {label} ---")
        print(f"request : {json.dumps(payload)}")
        result = invoke(payload)
        print(f"response: {json.dumps(result, default=str)}")


def main() -> None:
    ids = load_ids()
    print(f"Region {REGION}\n")

    zip_bytes = build_zip()
    arn = deploy(ids, zip_bytes)

    ids["tool_lambda_arn"] = arn
    save_ids(ids)
    print("\ntool_lambda_arn saved to network_ids.json")

    run_integration()


if __name__ == "__main__":
    main()
