"""Package, deploy, and invoke the in-VPC data-loader Lambda (Task 9, ap-south-1).

End-to-end, idempotent:
  1. Ensure an S3 GATEWAY VPC endpoint (com.amazonaws.ap-south-1.s3) is
     associated with the private route table so the in-VPC Lambda can reach S3
     WITHOUT a NAT gateway (private subnets, no NAT, no S3 interface endpoint).
  2. Attach an inline ``s3:GetObject`` policy on the load bucket/prefix to the
     reused tool-lambda role (its baseline grants do not include S3 reads).
  3. Build a deployment zip: handler.py + a fresh copy of src/common/schema.py +
     psycopg2 (manylinux2014_x86_64, cp312 wheel for the Lambda runtime).
  4. Create/update the ``residency-chatbot-db-loader`` Lambda in the 2 private subnets
     + lambda-sg, reusing the tool-lambda role; timeout 300s / memory 1024MB.
  5. Invoke it once per table and print rows loaded per table.

Run:  uv run python infra/deploy_loader.py [--rows-source-note]
Requires the local AWS identity to have lambda/iam/ec2 (VPC endpoint) perms.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile

import boto3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (  # noqa: E402
    DATA_BUCKET,
    DATA_PREFIX,
    PROJECT,
    REGION,
    TAGS,
    TAGS_MAP,
    load_ids,
    save_ids,
)

HERE = os.path.dirname(__file__)
PKG_DIR = os.path.join(HERE, "load_db")
SCHEMA_SRC = os.path.join(HERE, "..", "src", "common", "schema.py")
DATA_DIR = os.path.normpath(os.path.join(HERE, "..", "data"))
BUILD_DIR = os.path.join(HERE, "_loader_build")
ZIP_PATH = os.path.join(HERE, "_load_db.zip")

FUNCTION_NAME = f"{PROJECT}-db-loader"
RUNTIME = "python3.12"
PSYCOPG_PKG = "psycopg2-binary==2.9.9"

LOAD_BUCKET = DATA_BUCKET
LOAD_PREFIX = DATA_PREFIX
TABLES = ("applications", "subsidy", "installation", "inspection")

INLINE_POLICY_NAME = f"{PROJECT}-loader-s3-read"

lam = boto3.client("lambda", region_name=REGION)
ec2 = boto3.client("ec2", region_name=REGION)
iam = boto3.client("iam", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)


def ensure_data_bucket() -> None:
    """Create the demo-data bucket (in-region) if missing, then upload data/*.csv."""
    try:
        s3.head_bucket(Bucket=LOAD_BUCKET)
        print(f"[skip] data bucket exists: {LOAD_BUCKET}")
    except s3.exceptions.ClientError:
        print(f"[create] data bucket: {LOAD_BUCKET} ({REGION})")
        s3.create_bucket(
            Bucket=LOAD_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )
        s3.get_waiter("bucket_exists").wait(Bucket=LOAD_BUCKET)
        s3.put_bucket_tagging(
            Bucket=LOAD_BUCKET,
            Tagging={"TagSet": TAGS},
        )

    for table in TABLES:
        local = os.path.join(DATA_DIR, f"{table}.csv")
        if not os.path.isfile(local):
            raise SystemExit(
                f"sample data not found: {local}\n"
                f"Generate it first: uv run python tools/make_sample_data.py"
            )
        key = f"{LOAD_PREFIX}/{table}.csv"
        s3.upload_file(local, LOAD_BUCKET, key)
        print(f"[upload] {local} -> s3://{LOAD_BUCKET}/{key}")


def ensure_s3_read_policy(ids: dict) -> None:
    """Attach an inline s3:GetObject policy on the load bucket/prefix to the role."""
    role_arn = ids["tool_lambda_role_arn"]
    role_name = role_arn.split("/")[-1]
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "LoaderS3Read",
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": [f"arn:aws:s3:::{LOAD_BUCKET}/{LOAD_PREFIX}/*"],
            },
            {
                "Sid": "LoaderS3List",
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": [f"arn:aws:s3:::{LOAD_BUCKET}"],
                "Condition": {"StringLike": {"s3:prefix": [f"{LOAD_PREFIX}/*"]}},
            },
        ],
    }
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName=INLINE_POLICY_NAME,
        PolicyDocument=json.dumps(policy),
    )
    print(f"[ok] inline policy {INLINE_POLICY_NAME} on role {role_name}")


def build_zip() -> bytes:
    """Build the zip: handler + fresh schema.py copy + psycopg2 (manylinux wheel)."""
    if os.path.isdir(BUILD_DIR):
        shutil.rmtree(BUILD_DIR)
    os.makedirs(BUILD_DIR)

    print(f"[build] uv pip install {PSYCOPG_PKG} (manylinux2014_x86_64, cp312)")
    subprocess.check_call([
        "uv", "pip", "install",
        "--python-platform", "x86_64-manylinux2014",
        "--python-version", "3.12",
        "--only-binary", ":all:",
        "--target", BUILD_DIR,
        PSYCOPG_PKG,
    ])

    shutil.copy(os.path.join(PKG_DIR, "handler.py"),
                os.path.join(BUILD_DIR, "handler.py"))
    # schema.py is the single source of truth — copy it in fresh at build time.
    shutil.copy(SCHEMA_SRC, os.path.join(BUILD_DIR, "schema.py"))

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


def deploy(ids: dict, zip_bytes: bytes) -> None:
    env = {
        "Variables": {
            "DB_ENDPOINT": ids["db_endpoint"],
            "DB_NAME": ids["db_name"],
            "DB_PORT": str(ids.get("db_port", 5432)),
            "MASTER_SECRET_ARN": ids["db_master_secret_arn"],
            "LOAD_BUCKET": LOAD_BUCKET,
            "LOAD_PREFIX": LOAD_PREFIX,
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
            Timeout=300,
            MemorySize=1024,
            Environment=env,
            VpcConfig=vpc_config,
        )
        _wait_updated()
    except lam.exceptions.ResourceNotFoundException:
        print(f"[create] {FUNCTION_NAME}")
        for attempt in range(6):
            try:
                lam.create_function(
                    FunctionName=FUNCTION_NAME,
                    Runtime=RUNTIME,
                    Role=role_arn,
                    Handler="handler.handler",
                    Code={"ZipFile": zip_bytes},
                    Timeout=300,
                    MemorySize=1024,
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


def _wait_active() -> None:
    lam.get_waiter("function_active_v2").wait(FunctionName=FUNCTION_NAME)


def _wait_updated() -> None:
    lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION_NAME)


def invoke_table(table: str) -> dict:
    print(f"[invoke] {FUNCTION_NAME} table={table} (sync)")
    resp = lam.invoke(
        FunctionName=FUNCTION_NAME,
        InvocationType="RequestResponse",
        Payload=json.dumps({"table": table}).encode(),
    )
    payload = resp["Payload"].read().decode()
    if resp.get("FunctionError"):
        print(f"[error] table={table} FunctionError={resp['FunctionError']}")
        print(payload)
        raise SystemExit(1)
    return json.loads(payload)


def main() -> None:
    ids = load_ids()
    print(f"Region {REGION}  bucket s3://{LOAD_BUCKET}/{LOAD_PREFIX}/\n")

    # The S3 gateway VPC endpoint is created by provision_network.py.
    ensure_data_bucket()
    ensure_s3_read_policy(ids)
    # Give the inline policy a moment to propagate before the Lambda reads S3.
    time.sleep(10)

    zip_bytes = build_zip()
    deploy(ids, zip_bytes)

    results: dict[str, int] = {}
    for table in TABLES:
        summary = invoke_table(table)
        results[table] = summary.get("rows_loaded", -1)
        print(f"  -> {json.dumps(summary)}")

    ids["data_bucket"] = LOAD_BUCKET
    ids["data_prefix"] = LOAD_PREFIX
    save_ids(ids)

    print("\n=== rows loaded per table ===")
    for table, n in results.items():
        print(f"  {table:13s} {n}")


if __name__ == "__main__":
    main()
