"""Poll the Aurora cluster + instance until available, capture the writer endpoint
and the managed master-credential secret ARN into infra/network_ids.json.

Run:  uv run python infra/wait_aurora.py
"""
from __future__ import annotations

import os
import sys
import time

import boto3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import PROJECT, REGION, load_ids, save_ids  # noqa: E402

CLUSTER_ID = f"{PROJECT}-aurora"
INSTANCE_ID = f"{PROJECT}-aurora-1"
TIMEOUT = 900

rds = boto3.client("rds", region_name=REGION)


def main():
    ids = load_ids()

    deadline = time.time() + TIMEOUT
    last = None
    while time.time() < deadline:
        c = rds.describe_db_clusters(DBClusterIdentifier=CLUSTER_ID)["DBClusters"][0]
        try:
            i = rds.describe_db_instances(DBInstanceIdentifier=INSTANCE_ID)["DBInstances"][0]
            istatus = i["DBInstanceStatus"]
        except Exception:
            istatus = "creating"
        status = (c["Status"], istatus)
        if status != last:
            print(f"  cluster={c['Status']}  instance={istatus}")
            last = status
        if c["Status"] == "available" and istatus == "available":
            endpoint = c["Endpoint"]
            port = c["Port"]
            secret_arn = c.get("MasterUserSecret", {}).get("SecretArn")
            ids.update({
                "db_endpoint": endpoint,
                "db_port": port,
                "db_master_secret_arn": secret_arn,
            })
            save_ids(ids)
            print("\nAURORA AVAILABLE")
            print(f"  endpoint   : {endpoint}:{port}")
            print(f"  secret ARN : {secret_arn}")
            print(f"  publicly accessible: {i.get('PubliclyAccessible')}")
            return 0
        time.sleep(20)
    print("TIMEOUT waiting for Aurora")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
