"""Clean up / destroy ALL resources deployed by deploy.py for this project
(ap-south-1).

Deletes the resources created by deploy.py, in reverse dependency order, reading
their ids from infra/network_ids.json. Every deletion is guarded (missing/already
-deleted resources are skipped), so this is safe to re-run.

    uv run python cleanup.py                     # prompts for target account + confirmation
    uv run python cleanup.py --account 123456789012 --yes
    uv run python cleanup.py --yes --delete-bucket

On start it asks which AWS account to clean and verifies your active credentials
resolve to that account (aborting on mismatch), so you never destroy the wrong
account's stack. The region is always ap-south-1 (Mumbai).

This is DESTRUCTIVE. It removes the VPC, Aurora, Lambdas, ECR repo, Gateway,
Memory, APIs, Amplify app, IAM roles, DynamoDB table, and read-only DB secret
for this project. CloudWatch log groups are kept (cheap; useful for post-mortem).
The demo-data S3 bucket is kept unless --delete-bucket is passed.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "infra"))
from config import ACCOUNT, DATA_BUCKET, IDS_PATH, PROJECT, REGION, load_ids  # noqa: E402

ids = load_ids()


def _try(label: str, fn) -> None:
    try:
        fn()
        print(f"[del] {label}")
    except ClientError as e:
        print(f"[skip] {label}: {e.response['Error'].get('Code', 'error')}")
    except Exception as e:  # noqa: BLE001
        print(f"[skip] {label}: {type(e).__name__}: {e}")


def confirm_target_account(supplied: str | None) -> None:
    """Ask which account to clean, then verify credentials resolve to it."""
    print(f"\nCleanup region is FIXED to {REGION} (Mumbai).")
    target = (supplied or "").strip()
    if not target:
        try:
            target = input("Enter the target AWS account id to clean up: ").strip()
        except EOFError:
            raise SystemExit(
                "No account id provided. Re-run interactively or pass --account <id>."
            )
    if not (target.isdigit() and len(target) == 12):
        raise SystemExit(f"'{target}' is not a valid 12-digit AWS account id.")
    if target != ACCOUNT:
        raise SystemExit(
            f"ACCOUNT MISMATCH: you asked to clean up {target}, but your active AWS "
            f"credentials belong to {ACCOUNT}. Switch credentials/profile to the "
            f"target account (e.g. set AWS_PROFILE) and re-run."
        )
    print(f"Confirmed: cleaning up account {target} in {REGION}.")


def cleanup_amplify() -> None:
    if not ids.get("amplify_app_id"):
        return
    amp = boto3.client("amplify", region_name=REGION)
    _try(f"amplify app {ids['amplify_app_id']}",
         lambda: amp.delete_app(appId=ids["amplify_app_id"]))


def cleanup_apis() -> None:
    if ids.get("rest_api_id"):
        apigw = boto3.client("apigateway", region_name=REGION)
        _try(f"REST API {ids['rest_api_id']}",
             lambda: apigw.delete_rest_api(restApiId=ids["rest_api_id"]))
    if ids.get("ws_api_id"):
        apigw2 = boto3.client("apigatewayv2", region_name=REGION)
        _try(f"WebSocket API {ids['ws_api_id']}",
             lambda: apigw2.delete_api(ApiId=ids["ws_api_id"]))


def cleanup_lambdas() -> None:
    lam = boto3.client("lambda", region_name=REGION)
    for fn in (f"{PROJECT}-agent", f"{PROJECT}-tool", f"{PROJECT}-db-bootstrap",
               f"{PROJECT}-db-loader", f"{PROJECT}-ws-connect", f"{PROJECT}-ws-disconnect"):
        _try(f"lambda {fn}", lambda fn=fn: lam.delete_function(FunctionName=fn))


def cleanup_ecr() -> None:
    ecr = boto3.client("ecr", region_name=REGION)
    _try(f"ecr repo {PROJECT}-agent",
         lambda: ecr.delete_repository(repositoryName=f"{PROJECT}-agent", force=True))


def cleanup_agentcore() -> None:
    acc = boto3.client("bedrock-agentcore-control", region_name=REGION)
    gid = ids.get("gateway_id")
    if gid:
        tid = ids.get("gateway_target_id")
        if tid:
            _try(f"gateway target {tid}",
                 lambda: acc.delete_gateway_target(gatewayIdentifier=gid, targetId=tid))
            time.sleep(3)
        _try(f"gateway {gid}", lambda: acc.delete_gateway(gatewayIdentifier=gid))
    if ids.get("memory_id"):
        _try(f"memory {ids['memory_id']}",
             lambda: acc.delete_memory(memoryId=ids["memory_id"]))


def cleanup_aurora() -> None:
    rds = boto3.client("rds", region_name=REGION)
    inst = f"{PROJECT}-aurora-1"
    clus = f"{PROJECT}-aurora"
    _try(f"db instance {inst}",
         lambda: rds.delete_db_instance(DBInstanceIdentifier=inst, SkipFinalSnapshot=True))
    # wait for the instance to go before the cluster can be deleted
    try:
        rds.get_waiter("db_instance_deleted").wait(
            DBInstanceIdentifier=inst, WaiterConfig={"Delay": 20, "MaxAttempts": 60})
    except Exception:  # noqa: BLE001
        pass
    _try(f"db cluster {clus}",
         lambda: rds.delete_db_cluster(DBClusterIdentifier=clus, SkipFinalSnapshot=True))
    try:
        rds.get_waiter("db_cluster_deleted").wait(
            DBClusterIdentifier=clus, WaiterConfig={"Delay": 20, "MaxAttempts": 60})
    except Exception:  # noqa: BLE001
        pass
    _try(f"db subnet group {PROJECT}-db-subnet-group",
         lambda: rds.delete_db_subnet_group(DBSubnetGroupName=f"{PROJECT}-db-subnet-group"))


def cleanup_ddb() -> None:
    ddb = boto3.client("dynamodb", region_name=REGION)
    _try(f"dynamodb {PROJECT}-connections",
         lambda: ddb.delete_table(TableName=f"{PROJECT}-connections"))


def cleanup_secret() -> None:
    sm = boto3.client("secretsmanager", region_name=REGION)
    _try(f"secret {PROJECT}-db-readonly",
         lambda: sm.delete_secret(SecretId=f"{PROJECT}-db-readonly",
                                  ForceDeleteWithoutRecovery=True))


def cleanup_iam() -> None:
    iam = boto3.client("iam", region_name=REGION)
    roles = [f"{PROJECT}-tool-lambda-role", f"{PROJECT}-agent-lambda-role",
             f"{PROJECT}-gateway-role", f"{PROJECT}-ws-helper-role"]
    for role in roles:
        try:
            for p in iam.list_role_policies(RoleName=role).get("PolicyNames", []):
                iam.delete_role_policy(RoleName=role, PolicyName=p)
            for ap in iam.list_attached_role_policies(RoleName=role).get("AttachedPolicies", []):
                iam.detach_role_policy(RoleName=role, PolicyArn=ap["PolicyArn"])
            iam.delete_role(RoleName=role)
            print(f"[del] iam role {role}")
        except ClientError as e:
            print(f"[skip] iam role {role}: {e.response['Error'].get('Code')}")


def cleanup_network() -> None:
    ec2 = boto3.client("ec2", region_name=REGION)
    vpc_id = ids.get("vpc_id")
    if not vpc_id:
        return
    # VPC endpoints
    eps = ec2.describe_vpc_endpoints(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["VpcEndpoints"]
    if eps:
        _try("vpc endpoints",
             lambda: ec2.delete_vpc_endpoints(VpcEndpointIds=[e["VpcEndpointId"] for e in eps]))
        time.sleep(5)
    # subnets
    for sid in ids.get("private_subnet_ids", []):
        _try(f"subnet {sid}", lambda sid=sid: ec2.delete_subnet(SubnetId=sid))
    # route table (disassociate first)
    rt = ids.get("private_route_table_id")
    if rt:
        try:
            assoc = ec2.describe_route_tables(RouteTableIds=[rt])["RouteTables"][0].get("Associations", [])
            for a in assoc:
                if a.get("RouteTableAssociationId"):
                    ec2.disassociate_route_table(AssociationId=a["RouteTableAssociationId"])
        except Exception:  # noqa: BLE001
            pass
        _try(f"route table {rt}", lambda: ec2.delete_route_table(RouteTableId=rt))
    # security groups (retry — dependencies clear asynchronously)
    for key in ("aurora_sg", "lambda_sg", "vpce_sg"):
        sg = ids.get(key)
        if sg:
            for _ in range(6):
                try:
                    ec2.delete_security_group(GroupId=sg)
                    print(f"[del] sg {sg}")
                    break
                except ClientError as e:
                    if "DependencyViolation" in str(e):
                        time.sleep(10)
                        continue
                    print(f"[skip] sg {sg}: {e.response['Error'].get('Code')}")
                    break
    _try(f"vpc {vpc_id}", lambda: ec2.delete_vpc(VpcId=vpc_id))


def cleanup_bucket() -> None:
    s3 = boto3.resource("s3", region_name=REGION)
    bucket = s3.Bucket(DATA_BUCKET)
    _try(f"empty bucket {DATA_BUCKET}", lambda: bucket.objects.all().delete())
    _try(f"delete bucket {DATA_BUCKET}", lambda: bucket.delete())


def _clear_ids_state() -> None:
    """Remove the local runtime-state file so a later deploy starts fresh."""
    try:
        if os.path.isfile(IDS_PATH):
            os.remove(IDS_PATH)
            print(f"[del] local state {os.path.basename(IDS_PATH)}")
    except OSError as e:
        print(f"[skip] local state: {e}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Destroy the MNRE chatbot stack")
    ap.add_argument("--account", default=None,
                    help="target AWS account id (skips the interactive prompt)")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    ap.add_argument("--delete-bucket", action="store_true",
                    help="also delete the demo-data S3 bucket")
    ap.add_argument("--keep-state", action="store_true",
                    help="do not delete the local network_ids.json after cleanup")
    args = ap.parse_args()

    confirm_target_account(args.account)

    print(f"\nRegion {REGION}  Project {PROJECT}")
    print("This DELETES the chatbot stack (VPC, Aurora, Lambdas, ECR, Gateway, "
          "Memory, APIs, Amplify, IAM, DynamoDB, DB secret).")
    if not args.yes:
        if input("Type 'delete' to proceed: ").strip().lower() != "delete":
            print("aborted.")
            return

    cleanup_amplify()
    cleanup_apis()
    cleanup_agentcore()
    cleanup_lambdas()
    cleanup_ecr()
    cleanup_aurora()
    cleanup_ddb()
    cleanup_secret()
    cleanup_network()
    cleanup_iam()
    if args.delete_bucket:
        cleanup_bucket()
    if not args.keep_state:
        _clear_ids_state()

    print("\nCleanup complete (best-effort). Review the console for any lingering resources.")


if __name__ == "__main__":
    main()
