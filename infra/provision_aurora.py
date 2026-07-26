"""Provision Aurora PostgreSQL Serverless v2 (private) for the MNRE chatbot
(ap-south-1). The Secrets Manager interface endpoint is created by
provision_network.py; this script creates the database tier.

Idempotent. Creates:
  - a DB subnet group over the 2 private subnets
  - an Aurora PostgreSQL Serverless v2 cluster (min 0.5 ACU), PubliclyAccessible=false,
    in aurora-sg, with a master credential auto-stored in Secrets Manager
  - one serverless instance in the cluster

Reads infra/network_ids.json. Appends cluster/secret ids to that file.
Run infra/wait_aurora.py afterwards to poll for availability + capture endpoint.

Run:  uv run python infra/provision_aurora.py
"""
from __future__ import annotations

import os
import sys

import boto3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import PROJECT, REGION, TAGS, load_ids, save_ids  # noqa: E402

CLUSTER_ID = f"{PROJECT}-aurora"
INSTANCE_ID = f"{PROJECT}-aurora-1"
DB_NAME = "mnre"
MASTER_USER = "mnre_admin"
SUBNET_GROUP = f"{PROJECT}-db-subnet-group"

rds = boto3.client("rds", region_name=REGION)


def ensure_subnet_group(ids):
    try:
        rds.describe_db_subnet_groups(DBSubnetGroupName=SUBNET_GROUP)
        print(f"[skip] db subnet group exists: {SUBNET_GROUP}")
    except rds.exceptions.DBSubnetGroupNotFoundFault:
        rds.create_db_subnet_group(
            DBSubnetGroupName=SUBNET_GROUP,
            DBSubnetGroupDescription="MNRE chatbot private subnets",
            SubnetIds=ids["private_subnet_ids"],
            Tags=TAGS,
        )
        print(f"[create] db subnet group: {SUBNET_GROUP}")


def _pick_engine_version():
    # Prefer the latest standard aurora-postgresql 16.x (exclude 'limitless'
    # variants — they require a different cluster scalability type).
    resp = rds.describe_db_engine_versions(Engine="aurora-postgresql")
    candidates = [
        v["EngineVersion"] for v in resp["DBEngineVersions"]
        if "limitless" not in v["EngineVersion"].lower()
    ]

    def _key(v):
        parts = []
        for p in v.split("."):
            num = "".join(ch for ch in p if ch.isdigit())
            parts.append(int(num) if num else 0)
        return parts

    sixteen = sorted([c for c in candidates if c.startswith("16.")], key=_key)
    chosen = sixteen[-1] if sixteen else sorted(candidates, key=_key)[-1]
    return chosen


def ensure_cluster(ids):
    try:
        c = rds.describe_db_clusters(DBClusterIdentifier=CLUSTER_ID)["DBClusters"][0]
        print(f"[skip] cluster exists: {CLUSTER_ID} status={c['Status']}")
        return c
    except rds.exceptions.DBClusterNotFoundFault:
        pass
    version = _pick_engine_version()
    print(f"[create] Aurora PostgreSQL Serverless v2 cluster {CLUSTER_ID} (engine {version})")
    rds.create_db_cluster(
        DBClusterIdentifier=CLUSTER_ID,
        Engine="aurora-postgresql",
        EngineVersion=version,
        DatabaseName=DB_NAME,
        MasterUsername=MASTER_USER,
        ManageMasterUserPassword=True,  # auto-store master secret in Secrets Manager
        DBSubnetGroupName=SUBNET_GROUP,
        VpcSecurityGroupIds=[ids["aurora_sg"]],
        ServerlessV2ScalingConfiguration={"MinCapacity": 0.5, "MaxCapacity": 4.0},
        StorageEncrypted=True,
        Tags=TAGS,
    )
    return rds.describe_db_clusters(DBClusterIdentifier=CLUSTER_ID)["DBClusters"][0]


def ensure_instance():
    try:
        rds.describe_db_instances(DBInstanceIdentifier=INSTANCE_ID)
        print(f"[skip] instance exists: {INSTANCE_ID}")
        return
    except rds.exceptions.DBInstanceNotFoundFault:
        pass
    print(f"[create] serverless instance {INSTANCE_ID} (db.serverless)")
    rds.create_db_instance(
        DBInstanceIdentifier=INSTANCE_ID,
        DBClusterIdentifier=CLUSTER_ID,
        Engine="aurora-postgresql",
        DBInstanceClass="db.serverless",
        PubliclyAccessible=False,
        Tags=TAGS,
    )


def main():
    ids = load_ids()
    print(f"Region {REGION}\n")
    ensure_subnet_group(ids)
    ensure_cluster(ids)
    ensure_instance()

    ids.update({
        "db_cluster_id": CLUSTER_ID,
        "db_instance_id": INSTANCE_ID,
        "db_name": DB_NAME,
        "master_username": MASTER_USER,
        "db_subnet_group": SUBNET_GROUP,
    })
    save_ids(ids)
    print("\nProvisioning initiated. Cluster + instance take a few minutes to become available.")
    print("Run infra/wait_aurora.py to poll for availability + capture endpoint/secret ARN.")


if __name__ == "__main__":
    main()
